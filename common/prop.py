""" Base of Input/Output Properties. """

import itertools
from abc import ABC, abstractmethod
from typing import Tuple, List, Optional, Iterable

import torch
from torch import Tensor

from DiffAbs.DiffAbs import AbsDom, AbsEle, ConcDist, deeppoly
from DiffAbs.DiffAbs.utils import valid_lb_ub

device = torch.device('cuda:2' if torch.cuda.is_available() else 'cpu')
# device = torch.device('cuda' if torch.cuda.is_available() else 'cpu')
# device = torch.device('cpu')

class AbsProp(ABC):
    """ All encoded properties should provide safe distance function and violation distance function.
        This distance function can be used to further compute losses in training and verification.
        It means: safe(violation) dist=0 means safe(violation) proved by over-approximation.
        Moreover, dist means how much until it becomes safe(violation).
    """
    def __init__(self, name: str):
        """
        :param name: property name
        """
        self.name = name
        return

    @abstractmethod
    def lbub(self, device=None) -> Tuple[Tensor, Tensor]:
        """ Return the lower bound / upper bound tensors tuple. """
        raise NotImplementedError()

    def xdim(self) -> int:
        """ Return the dimension of input/state. """
        lb, _ = self.lbub()
        shape = lb.shape[1:] # not include batch dimension
        return torch.prod(torch.tensor(shape)).item()

    def safe_dist(self, outs: AbsEle, *args, **kwargs):
        """ Return the safety distance with the guarantee that dist == 0 => safe. """
        raise NotImplementedError()
    
    def safe_feature_dist(self, outs: AbsEle, *args, **kwargs) -> Tensor:
        """ Return the safety distance with the guarantee that dist == 0 => safe for feature. """
        raise NotImplementedError()

    def safe_sheep(self, outs: AbsEle, *args, **kwargs) -> Tensor:
        """ Return a "black sheep" state of safety property such that sheep is safe => all safe.
            The black sheep can be used in more diverse optimization metrics (safe_dist() is only L1 norm).
        """
        raise NotImplementedError()

    def viol_dist(self, outs: AbsEle, *args, **kwargs):
        """ Return the safety distance with the guarantee that dist == 0 => violation. """
        raise NotImplementedError()

    def viol_sheep(self, outs: AbsEle, *args, **kwargs) -> Tensor:
        """ Return a "black sheep" state of safety property such that sheep is violating => all violating.
            The black sheep can be used in more diverse optimization metrics (viol_dist() is only L1 norm).
        """
        raise NotImplementedError()

    def safe_dist_conc(self, outs: Tensor, *args, **kwargs):
        """ Return the concrete safety distance of a state but not abstraction. """
        raise NotImplementedError()

    def viol_dist_conc(self, outs: Tensor, *args, **kwargs):
        """ Return the concrete violation distance of a state but not abstraction. """
        raise NotImplementedError()

    def tex(self) -> str:
        """ Return the property name in tex format for pretty printing. """
        return self.name
    pass


class OneProp(AbsProp):
    """ One specific property that calls corresponding safety/violation distance methods. """

    def __init__(self, name: str, dom: Optional[AbsDom], safe_fn: str, viol_fn: str, fn_args: Iterable):
        """
        :param dom: the abstract domain to incur corresponding safety/violation functions,
                    can be None if not calling those functions (e.g., just querying init/safety constraints)
        :param safe_fn: the method name to call safety distance related functions
        :param viol_fn: the method name to call violation distance related functions
        :param fn_args: a tuple of extra method arguments for safety/violation functions
        """
        super().__init__(name)
        self.dom = dom
        self.safe_fn = safe_fn
        self.viol_fn = viol_fn
        self.fn_args = fn_args
        return

    def safe_dist(self, outs: AbsEle, *args, **kwargs):
        """ Return the safety distance with the guarantee that dist == 0 => safe. """
        return getattr(self.dom.Dist(), self.safe_fn)(outs, *self.fn_args)
    
    def safe_dist_support(self, outs: AbsEle, *args, **kwargs):
        return getattr(deeppoly.Dist, 'cols_is_many_times')(outs, *self.fn_args)

    def viol_dist(self, outs: AbsEle, *args, **kwargs):
        """ Return the safety distance with the guarantee that dist == 0 => violation. """
        return getattr(self.dom.Dist(), self.viol_fn)(outs, *self.fn_args)

    def safe_dist_conc(self, outs: Tensor, *args, **kwargs):
        """ Return the concrete safety distance of a state but not abstraction. """
        return getattr(ConcDist, self.safe_fn)(outs, *self.fn_args)

    def viol_dist_conc(self, outs: Tensor, *args, **kwargs):
        """ Return the concrete violation distance of a state but not abstraction. """
        return getattr(ConcDist, self.viol_fn)(outs, *self.fn_args)
    pass


class AndProp(AbsProp):
    """ Conjunction of a collection of AbsProps. """

    def __init__(self, props: List[AbsProp]):
        assert len(props) > 0
        xdims = [p.xdim() for p in props]
        assert all([d == xdims[0] for d in xdims]) #输入维度要保持一致 TODO why?

        super().__init__('&'.join([p.name for p in props]))

        self.props = props
        self.lb, self.ub, self.labels = self.join_all(props)
        return

    def tex(self) -> str:
        names = [p.tex() for p in self.props]
        unique_names = []
        for n in names:
            if n not in unique_names:
                unique_names.append(n)
        return ' \\land '.join(unique_names)

    def join_all(self, props: List[AbsProp]):
        """ Conjoin multiple properties altogether. Now that each property may have different input space and different
            safety / violation distance functions. This method will re-arrange and determine the boundaries of sub-
            regions and which properties they should satisfy.
        """
        nprops = len(props)
        assert nprops > 0

        # initialize for 1st prop

        indentical_name = self.props[0].tex()
        if 'label' in indentical_name: # mnist or cifar
            nprops = props[0].LABEL_NUMBER
            orig_label = torch.eye(nprops).byte()
        else: #acas
            orig_label = torch.eye(nprops).byte()  # showing each input region which properties they should obey
        lbs, ubs = props[0].lbub(device=device) # both are 1 * input_dim
        if 'label' in indentical_name:
            zeroth = props[0].fn_args[0]
            labels = orig_label[[zeroth]].expand(len(lbs), nprops).to(device) # for 1st prob, labels is 1 * nprob matrix [[1,0,0,...]]
        else:
            labels = orig_label[[0]].expand(len(lbs), nprops).to(device) # for 1st prob, labels is 1 * nprob matrix [[1,0,0,...]] 

        for i, prop in enumerate(props):
            if i == 0:
                continue

            new_lbs, new_ubs = prop.lbub(device=device)
            assert valid_lb_ub(new_lbs, new_ubs)
            if 'label' in indentical_name:
                i = props[i].fn_args[0]
            new_labels = orig_label[[i]].expand(len(new_lbs), nprops).to(device)
            if 'feature' not in indentical_name:
                lbs, ubs, labels = self._join(lbs, ubs, labels, new_lbs, new_ubs, new_labels)
            else:
                # cat the new lbs, ubs, labels to the end of the old ones
                lbs = torch.cat((lbs, new_lbs), dim=0)
                ubs = torch.cat((ubs, new_ubs), dim=0)
                labels = torch.cat((labels, new_labels), dim=0)
        return lbs, ubs, labels

    def _join(self, x_lbs: Tensor, x_ubs: Tensor, x_labels: Tensor,
              y_lbs: Tensor, y_ubs: Tensor, y_labels: Tensor) -> Tuple[Tensor, Tensor, Tensor]:
        """
        Algorithm: Keep searching for "new" intersections, and refine by it, until none is found.

        We assume X and Y are mutually exclusive within themselves. Here shared_xxx keeps the intersected ones from
        X and Y. Therefore, shared_xxx won't intersect with anyone from X or Y anymore. Because x1 and y1 and x2 = empty.

        All arguments are assumed to be batched tensors.
        """
        # 上面这段话的意思好像是把X和Y的交集取出来了，这样这个交集和剩下的余集就是互不相交的了
        shared_lbs, shared_ubs, shared_labels = [], [], []  # intersected ones from X and Y

        def _covered(new_lb: Tensor, new_ub: Tensor, new_label: Tensor) -> bool:
            """
            Returns True if the new LB/UB is already covered by some intersected piece. Assuming new_lb/new_ub is
            from X or Y. So there won't be intersection, thus just check subset? is sufficient.

            Assuming all params are not-batched.
            """
            for i in range(len(shared_lbs)):
                shared_lb, shared_ub, shared_label = shared_lbs[i], shared_ubs[i], shared_labels[i]
                if valid_lb_ub(shared_lb, new_lb) and valid_lb_ub(new_ub, shared_ub):
                    assert torch.equal(new_label | shared_label, shared_label), 'New intersected cube got more props?!'
                    return True
            return False

        while True:
            found_new_shared = False
            for i, j in itertools.product(range(len(x_lbs)), range(len(y_lbs))): #(1,share_labels)
                xlb, xub, xlabel = x_lbs[i], x_ubs[i], x_labels[i]
                ylb, yub, ylabel = y_lbs[j], y_ubs[j], y_labels[j]
                try:
                    new_shared_lb, new_shared_ub = lbub_intersect(xlb, xub, ylb, yub)
                    new_shared_label = xlabel | ylabel
                except ValueError:
                    continue

                if _covered(new_shared_lb, new_shared_ub, new_shared_label):
                    # Has been found before.
                    # Possible when a sub-piece x11 from X (due to x1 intersects y1) is comparing with y1 again.
                    continue

                # save new intersected cube
                found_new_shared = True
                shared_lbs.append(new_shared_lb)
                shared_ubs.append(new_shared_ub)
                shared_labels.append(new_shared_label)

                # replace x by split non-intersected boxes in the X list
                rest_x_lbs, rest_x_ubs = lbub_exclude(xlb, xub, new_shared_lb, new_shared_ub)
                # rest_x_lbs = rest_x_lbs.to(device)
                # rest_x_ubs = rest_x_ubs.to(device)
                rest_x_labels = xlabel.unsqueeze(dim=0).expand(len(rest_x_lbs), *xlabel.size()).to(device)
                x_lbs = torch.cat((x_lbs[:i], rest_x_lbs, x_lbs[i+1:]), dim=0)
                x_ubs = torch.cat((x_ubs[:i], rest_x_ubs, x_ubs[i+1:]), dim=0)
                x_labels = torch.cat((x_labels[:i], rest_x_labels, x_labels[i+1:]), dim=0)

                # replace y by split non-intersected boxes in the Y list
                rest_y_lbs, rest_y_ubs = lbub_exclude(ylb, yub, new_shared_lb, new_shared_ub)
                rest_y_labels = ylabel.unsqueeze(dim=0).expand(len(rest_y_lbs), *ylabel.size())
                y_lbs = torch.cat((y_lbs[:j], rest_y_lbs, y_lbs[j+1:]), dim=0)
                y_ubs = torch.cat((y_lbs[:j], rest_y_ubs, y_ubs[j+1:]), dim=0)
                y_labels = torch.cat((y_labels[:j], rest_y_labels, y_labels[j+1:]), dim=0)
                break

            if not found_new_shared:
                break

        shared_lbs = torch.stack(shared_lbs, dim=0) if len(shared_lbs) > 0 else Tensor()
        shared_ubs = torch.stack(shared_ubs, dim=0) if len(shared_ubs) > 0 else Tensor()
        shared_labels = torch.stack(shared_labels, dim=0) if len(shared_labels) > 0 else Tensor().byte()

        shared_lbs = shared_lbs.to(device)
        shared_ubs = shared_ubs.to(device)
        shared_labels = shared_labels.to(device)
        x_labels = x_labels.to(device)
        y_labels = y_labels.to(device)

        all_lbs = torch.cat((shared_lbs, x_lbs, y_lbs), dim=0)
        all_ubs = torch.cat((shared_ubs, x_ubs, y_ubs), dim=0)
        all_labels = torch.cat((shared_labels, x_labels, y_labels), dim=0)
        return all_lbs, all_ubs, all_labels

    def lbub(self, device=None) -> Tuple[Tensor, Tensor]:
        """
        :return: Tensor on CPU, need to move to GPU if necessary.
        """
        lb, ub = self.lb, self.ub
        if device is not None:
            lb, ub = lb.to(device), ub.to(device)
        return lb, ub

    def bitmap(self, device=None) -> Tensor:
        """ Return the bit tensor corresponding to default LB/UB, showing which properties they should satisfy. """
        r = self.labels
        if device is not None:
            r = r.to(device)
        return r

    def props_of(self, bitmap: Tensor) -> List[AbsProp]:
        """ Return the corresponding properties of certain indices. """
        idxs = bitmap.nonzero(as_tuple=True)[0]
        assert idxs.dim() == 1
        props = [self.props[i] for i in idxs]
        return props
    
    def safe_dist_support(self, outs: AbsEle, bitmap: Tensor, *args, **kwargs):
        """ sum(every prop's classes scores loss)
        :param bitmap: the bit-vectors corresponding to outputs, showing what rules they should obey
        """
        if len(self.props) == 1:
            # the only prop is the default one, which is always true
            assert torch.equal(bitmap, torch.ones_like(bitmap))
            # dists = self.props[0].safe_dist(outs, *args, **kwargs)
            dists = None
            return dists
        
        res = []

        for i,bits in enumerate(bitmap):
            bits = bits.nonzero(as_tuple=True)[0].tolist()
            piece_dists = deeppoly.Dist.cols_is_many_times(outs[i], bits, many_times=10, *args, **kwargs)
        
        
        # full_dists = torch.zeros(len(bitmap), *piece_dists.size()[1:], device=piece_dists.device)
        # full_dists.scatter_(0, bits, piece_dists)
            res.append(piece_dists)

        res = torch.stack(res, dim=-1)  # Batch x nprops
        return torch.sum(res, dim=-1)

    def safe_dist(self, outs: AbsEle, bitmap: Tensor, *args, **kwargs):
        """ sum(every prop's safe_dists)
        :param bitmap: the bit-vectors corresponding to outputs, showing what rules they should obey
        """
        if len(self.props) == 1:
            assert torch.equal(bitmap, torch.ones_like(bitmap))
            dists = self.props[0].safe_dist(outs, *args, **kwargs)
            return dists

        res = []
        if 'label' in self.props[0].tex():
            num_patches = self.labels.size()[-1]
        else:
            num_patches = len(self.props)
        # for i, prop in enumerate(self.props):
        for i in range(num_patches):
            if 'label' in self.props[0].tex():
                for _, prop in enumerate(self.props):
                    if prop.fn_args[0] == i:
                        break
            else:
                prop = self.props[i]
            bits = bitmap[..., i]
            if not bits.any():
                # no one here needs to obey this property
                continue

            ''' The default nonzero(as_tuple=True) returns a tuple, make scatter_() unhappy.
                Here we just extract the real data from it to make it the same as old nonzero().squeeze(dim=-1).
            '''
            bits = bits.nonzero(as_tuple=True)[0]
            assert bits.dim() == 1
            piece_outs = outs[bits]
            # if piece_outs._lcnst.shape[0] == 2:
            #     print('debug')
            piece_dists = prop.safe_dist(piece_outs, *args, **kwargs)
            full_dists = torch.zeros(len(bitmap), *piece_dists.size()[1:], device=piece_dists.device)
            full_dists.scatter_(0, bits, piece_dists)
            res.append(full_dists)

        res = torch.stack(res, dim=-1)  # Batch x nprops
        return torch.sum(res, dim=-1)

    def viol_dist(self, outs: AbsEle, bitmap: Tensor, *args, **kwargs):
        """ min(every prop's viol_dists)
        :param rules: the bit-vectors corresponding to outputs, showing what rules they should obey
        """
        res = []
        for i, prop in enumerate(self.props):
            bits = bitmap[..., i]
            if not bits.any():
                # no one here needs to obey this property
                continue

            ''' The default nonzero(as_tuple=True) returns a tuple, make scatter_() unhappy.
                Here we just extract the real data from it to make it the same as old nonzero().squeeze(dim=-1).
            '''
            bits = bits.nonzero(as_tuple=True)[0]
            assert bits.dim() == 1
            piece_outs = outs[bits]
            piece_dists = prop.viol_dist(piece_outs, *args, **kwargs)
            full_dists = torch.full((len(bitmap), *piece_dists.size()[1:]), float('inf'), device=piece_dists.device)
            full_dists.scatter_(0, bits, piece_dists)
            res.append(full_dists)

        res = torch.stack(res, dim=-1)  # Batch x nprops
        mins, _ = torch.min(res, dim=-1)
        return mins

    def viol_dist_conc(self, outs: Tensor, bitmap: Tensor, *args, **kwargs):
        """ min(every prop's viol_dists)
        :param bitmap: the bit-vectors corresponding to outputs, showing what rules they should obey
        """
        res = []
        for i, prop in enumerate(self.props):
            bits = bitmap[..., i]
            if not bits.any():
                # no one here needs to obey this property
                continue

            ''' The default nonzero(as_tuple=True) returns a tuple, make scatter_() unhappy.
                Here we just extract the real data from it to make it the same as old nonzero().squeeze(dim=-1).
            '''
            bits = bits.nonzero(as_tuple=True)[0]
            assert bits.dim() == 1
            piece_outs = outs[bits]
            piece_dists = prop.viol_dist_conc(piece_outs, *args, **kwargs)
            full_dists = torch.full((len(bitmap), *piece_dists.size()[1:]), float('inf'), device=piece_dists.device)
            full_dists.scatter_(0, bits, piece_dists)
            res.append(full_dists)

        res = torch.stack(res, dim=-1)  # Batch x nprops
        mins, _ = torch.min(res, dim=-1)
        return mins
    pass

class FeatureAndProp(AbsProp):

    """ Conjunction of a collection of AbsProps. """

    def __init__(self, props: List[AbsProp]):
        assert len(props) > 0
        xdims = [p.xdim() for p in props]
        assert all([d == xdims[0] for d in xdims]) #输入维度要保持一致 TODO why?

        super().__init__('&'.join([p.name for p in props]))

        self.props = props
        self.lb, self.ub, self.labels = self.join_all(props)
        return

    def tex(self) -> str:
        names = [p.tex() for p in self.props]
        unique_names = []
        for n in names:
            if n not in unique_names:
                unique_names.append(n)
        return ' \\land '.join(unique_names)

    def join_all(self, props: List[AbsProp]):
        """ Conjoin multiple properties altogether. Now that each property may have different input space and different
            safety / violation distance functions. This method will re-arrange and determine the boundaries of sub-
            regions and which properties they should satisfy.
        """

        nprops = len(props)
        assert nprops > 0

        # rewrite the labels to be 0, 1, 2, ...
        nprops = props[0].LABEL_NUMBER
        # initialize for 1st prop
        orig_label = torch.eye(nprops).byte()  # showing each input region which properties they should obey
        lbs, ubs = props[0].lbub() # both are 1 * input_dim
        labels = orig_label[props[0].fn_args].expand(len(lbs), nprops) # for 1st prob, labels is 1 * nprob matrix [[1,0,0,...]] 

        for i, prop in enumerate(props):
            if i == 0:
                continue

            new_lbs, new_ubs = prop.lbub()
            assert valid_lb_ub(new_lbs, new_ubs)
            new_labels = orig_label[props[i].fn_args].expand(len(new_lbs), nprops)

            lbs, ubs, labels = self._join(lbs, ubs, labels, new_lbs, new_ubs, new_labels)
        return lbs, ubs, labels

    def _join(self, x_lbs: Tensor, x_ubs: Tensor, x_labels: Tensor,
              y_lbs: Tensor, y_ubs: Tensor, y_labels: Tensor) -> Tuple[Tensor, Tensor, Tensor]:
        """
        Algorithm: Keep searching for "new" intersections, and refine by it, until none is found.

        We assume X and Y are mutually exclusive within themselves. Here shared_xxx keeps the intersected ones from
        X and Y. Therefore, shared_xxx won't intersect with anyone from X or Y anymore. Because x1 and y1 and x2 = empty.

        All arguments are assumed to be batched tensors.
        """
        # 上面这段话的意思好像是把X和Y的交集取出来了，这样这个交集和剩下的余集就是互不相交的了
        shared_lbs, shared_ubs, shared_labels = [], [], []  # intersected ones from X and Y

        def _covered(new_lb: Tensor, new_ub: Tensor, new_label: Tensor) -> bool:
            """
            Returns True if the new LB/UB is already covered by some intersected piece. Assuming new_lb/new_ub is
            from X or Y. So there won't be intersection, thus just check subset? is sufficient.

            Assuming all params are not-batched.
            """
            for i in range(len(shared_lbs)):
                shared_lb, shared_ub, shared_label = shared_lbs[i], shared_ubs[i], shared_labels[i]
                if valid_lb_ub(shared_lb, new_lb) and valid_lb_ub(new_ub, shared_ub):
                    assert torch.equal(new_label | shared_label, shared_label), 'New intersected cube got more props?!'
                    return True
            return False

        while True:
            found_new_shared = False
            for i, j in itertools.product(range(len(x_lbs)), range(len(y_lbs))): #(1,share_labels)
                xlb, xub, xlabel = x_lbs[i], x_ubs[i], x_labels[i]
                ylb, yub, ylabel = y_lbs[j], y_ubs[j], y_labels[j]
                try:
                    new_shared_lb, new_shared_ub = lbub_intersect(xlb, xub, ylb, yub)
                    new_shared_label = xlabel | ylabel
                except ValueError:
                    continue

                if _covered(new_shared_lb, new_shared_ub, new_shared_label):
                    # Has been found before.
                    # Possible when a sub-piece x11 from X (due to x1 intersects y1) is comparing with y1 again.
                    continue

                # save new intersected cube
                found_new_shared = True
                shared_lbs.append(new_shared_lb)
                shared_ubs.append(new_shared_ub)
                shared_labels.append(new_shared_label)

                # replace x by split non-intersected boxes in the X list
                rest_x_lbs, rest_x_ubs = lbub_exclude(xlb, xub, new_shared_lb, new_shared_ub)
                rest_x_labels = xlabel.unsqueeze(dim=0).expand(len(rest_x_lbs), *xlabel.size())
                x_lbs = torch.cat((x_lbs[:i], rest_x_lbs, x_lbs[i+1:]), dim=0)
                x_ubs = torch.cat((x_ubs[:i], rest_x_ubs, x_ubs[i+1:]), dim=0)
                x_labels = torch.cat((x_labels[:i], rest_x_labels, x_labels[i+1:]), dim=0)

                # replace y by split non-intersected boxes in the Y list
                rest_y_lbs, rest_y_ubs = lbub_exclude(ylb, yub, new_shared_lb, new_shared_ub)
                rest_y_labels = ylabel.unsqueeze(dim=0).expand(len(rest_y_lbs), *ylabel.size())
                y_lbs = torch.cat((y_lbs[:j], rest_y_lbs, y_lbs[j+1:]), dim=0)
                y_ubs = torch.cat((y_lbs[:j], rest_y_ubs, y_ubs[j+1:]), dim=0)
                y_labels = torch.cat((y_labels[:j], rest_y_labels, y_labels[j+1:]), dim=0)
                break

            if not found_new_shared:
                break

        shared_lbs = torch.stack(shared_lbs, dim=0) if len(shared_lbs) > 0 else Tensor()
        shared_ubs = torch.stack(shared_ubs, dim=0) if len(shared_ubs) > 0 else Tensor()
        shared_labels = torch.stack(shared_labels, dim=0) if len(shared_labels) > 0 else Tensor().byte()

        all_lbs = torch.cat((shared_lbs, x_lbs, y_lbs), dim=0)
        all_ubs = torch.cat((shared_ubs, x_ubs, y_ubs), dim=0)
        all_labels = torch.cat((shared_labels, x_labels, y_labels), dim=0)
        return all_lbs, all_ubs, all_labels

    def lbub(self, device=None) -> Tuple[Tensor, Tensor]:
        """
        :return: Tensor on CPU, need to move to GPU if necessary.
        """
        lb, ub = self.lb, self.ub
        if device is not None:
            lb, ub = lb.to(device), ub.to(device)
        return lb, ub

    def bitmap(self, device=None) -> Tensor:
        """ Return the bit tensor corresponding to default LB/UB, showing which properties they should satisfy. """
        r = self.labels
        if device is not None:
            r = r.to(device)
        return r

    def props_of(self, bitmap: Tensor) -> List[AbsProp]:
        """ Return the corresponding properties of certain indices. """
        idxs = bitmap.nonzero(as_tuple=True)[0]
        assert idxs.dim() == 1
        props = [self.props[i] for i in idxs]
        return props
    
    def safe_dist_support(self, outs: AbsEle, bitmap: Tensor, *args, **kwargs):
        """ sum(every prop's classes scores loss)
        :param bitmap: the bit-vectors corresponding to outputs, showing what rules they should obey
        """
        if len(self.props) == 1:
            # the only prop is the default one, which is always true
            assert torch.equal(bitmap, torch.ones_like(bitmap))
            # dists = self.props[0].safe_dist(outs, *args, **kwargs)
            dists = None
            return dists
        
        res = []

        for i,bits in enumerate(bitmap):
            bits = bits.nonzero(as_tuple=True)[0].tolist()
            piece_dists = deeppoly.Dist.cols_is_many_times(outs[i], bits, many_times=1, *args, **kwargs)
        
        
        # full_dists = torch.zeros(len(bitmap), *piece_dists.size()[1:], device=piece_dists.device)
        # full_dists.scatter_(0, bits, piece_dists)
            res.append(piece_dists)

        res = torch.stack(res, dim=-1)  # Batch x nprops
        return torch.sum(res, dim=-1)

    def safe_dist(self, outs: AbsEle, bitmap: Tensor, *args, **kwargs):
        """ sum(every prop's safe_dists)
        :param bitmap: the bit-vectors corresponding to outputs, showing what rules they should obey
        """
        if len(self.props) == 1:
            assert torch.equal(bitmap, torch.ones_like(bitmap))
            dists = self.props[0].safe_dist(outs, *args, **kwargs)
            return dists

        res = []
        for i, prop in enumerate(self.props):
            bits = bitmap[..., i]
            if not bits.any():
                # no one here needs to obey this property
                continue

            ''' The default nonzero(as_tuple=True) returns a tuple, make scatter_() unhappy.
                Here we just extract the real data from it to make it the same as old nonzero().squeeze(dim=-1).
            '''
            bits = bits.nonzero(as_tuple=True)[0]
            assert bits.dim() == 1
            piece_outs = outs[bits]
            piece_dists = prop.safe_dist(piece_outs, *args, **kwargs)
            full_dists = torch.zeros(len(bitmap), *piece_dists.size()[1:], device=piece_dists.device)
            full_dists.scatter_(0, bits, piece_dists)
            res.append(full_dists)

        res = torch.stack(res, dim=-1)  # Batch x nprops
        return torch.sum(res, dim=-1)
    
    def safe_feature_dist(self, outs: AbsEle, bitmap: Tensor, *args, **kwargs):
        """ sum(every prop's safe_dists)
        :param bitmap: the bit-vectors corresponding to outputs, showing what rules they should obey
        """
        if len(self.props) == 1:
            assert torch.equal(bitmap, torch.ones_like(bitmap))
            dists = self.props[0].safe_dist(outs, *args, **kwargs)
            return dists

        res = []
        num_feature_labels = self.labels.size()[-1]
        for i in range(num_feature_labels):
            bits = bitmap[..., i]
            # bits = torch.nonzero(bitmap[:, i] == 1).squeeze(1)
            # transfer bit to int8
            # bits = bits.type(torch.int8)
            if not bits.any():
                #no one here needs to obey this property
                continue

            ''' The default nonzero(as_tuple=True) returns a tuple, make scatter_() unhappy.
                Here we just extract the real data from it to make it the same as old nonzero().squeeze(dim=-1).
            '''
            bits = bits.nonzero(as_tuple=True)[0]
            assert bits.dim() == 1
            piece_outs = outs[bits]
            # piece_dists = prop.safe_dist(piece_outs, *args, **kwargs)
            piece_dists = deeppoly.Dist.cols_feature_is_max(piece_outs, i, *args, **kwargs)
            full_dists = torch.zeros(len(bitmap), *piece_dists.size()[1:], device=piece_dists.device)
            full_dists.scatter_(0, bits, piece_dists)
            res.append(full_dists)

        res = torch.stack(res, dim=-1)  # Batch x labels
        return torch.sum(res, dim=-1)
        # return torch.sum(res) # sum over all the dists to get a scalar value

    def viol_dist(self, outs: AbsEle, bitmap: Tensor, *args, **kwargs):
        """ min(every prop's viol_dists)
        :param rules: the bit-vectors corresponding to outputs, showing what rules they should obey
        """
        res = []
        for i, prop in enumerate(self.props):
            bits = bitmap[..., i]
            if not bits.any():
                # no one here needs to obey this property
                continue

            ''' The default nonzero(as_tuple=True) returns a tuple, make scatter_() unhappy.
                Here we just extract the real data from it to make it the same as old nonzero().squeeze(dim=-1).
            '''
            bits = bits.nonzero(as_tuple=True)[0]
            assert bits.dim() == 1
            piece_outs = outs[bits]
            piece_dists = prop.viol_dist(piece_outs, *args, **kwargs)
            full_dists = torch.full((len(bitmap), *piece_dists.size()[1:]), float('inf'), device=piece_dists.device)
            full_dists.scatter_(0, bits, piece_dists)
            res.append(full_dists)

        res = torch.stack(res, dim=-1)  # Batch x nprops
        mins, _ = torch.min(res, dim=-1)
        return mins

    def viol_dist_conc(self, outs: Tensor, bitmap: Tensor, *args, **kwargs):
        """ min(every prop's viol_dists)
        :param bitmap: the bit-vectors corresponding to outputs, showing what rules they should obey
        """
        res = []
        for i, prop in enumerate(self.props):
            bits = bitmap[..., i]
            if not bits.any():
                # no one here needs to obey this property
                continue

            ''' The default nonzero(as_tuple=True) returns a tuple, make scatter_() unhappy.
                Here we just extract the real data from it to make it the same as old nonzero().squeeze(dim=-1).
            '''
            bits = bits.nonzero(as_tuple=True)[0]
            assert bits.dim() == 1
            piece_outs = outs[bits]
            piece_dists = prop.viol_dist_conc(piece_outs, *args, **kwargs)
            full_dists = torch.full((len(bitmap), *piece_dists.size()[1:]), float('inf'), device=piece_dists.device)
            full_dists.scatter_(0, bits, piece_dists)
            res.append(full_dists)

        res = torch.stack(res, dim=-1)  # Batch x nprops
        mins, _ = torch.min(res, dim=-1)
        return mins
    pass

        
    

def lbub_intersect(lb1: Tensor, ub1: Tensor, lb2: Tensor, ub2: Tensor) -> Tuple[Tensor, Tensor]:
    """ Return intersected [lb1, ub1] logic-and [lb2, ub2], or raise ValueError when they do not overlap.
    :param lb1, ub1, lb2, ub2: not batched
    :return: not batched tensors
    """
    assert lb1.size() == lb2.size() and ub1.size() == ub2.size()
    if len(lb1.shape) == 3:
        # res_lb = torch.max(lb1, lb2)
        # res_ub = torch.min(ub1, ub2)

        if (lb1 >= ub2).all() or (lb2 >= ub1).all(): # no intersection
            raise ValueError('Intersection failed.')
        elif (lb1 >= lb2).all() and (ub1 <= ub2).all(): # 2 include 1
            res_lb = lb1
            res_ub = ub1
        elif (lb2 >= lb1).all() and (ub2 <= ub1).all(): # 1 include 2
            res_lb = lb2
            res_ub = ub2
        # 1 intersect 2 and 1 is right
        elif (lb1 >= lb2).all() and (lb1 <= ub2).all() and (ub1 >= ub2).all():
            res_lb = lb1
            res_ub = ub2
        elif (lb2 >= lb1).all() and (lb2 <= ub1).all() and (ub2 >= ub1).all():
            res_lb = lb2
            res_ub = ub1
        else:
            raise ValueError('Intersection failed.')
        
    else:
        res_lb, _ = torch.max(torch.stack((lb1, lb2), dim=-1), dim=-1)
        res_ub, _ = torch.min(torch.stack((ub1, ub2), dim=-1), dim=-1)

    if not valid_lb_ub(res_lb, res_ub):
        raise ValueError('Intersection failed.')
    return res_lb, res_ub


def lbub_exclude(lb1: Tensor, ub1: Tensor, lb2: Tensor, ub2: Tensor, accu_lb=Tensor().to(device=device), accu_ub=Tensor().to(device=device),
                 eps: float = 1e-6) -> Tuple[Tensor, Tensor]:
    """ Return set excluded [lb1, ub1] (-) [lb2, ub2].
        Assuming [lb2, ub2] is in [lb1, ub1].

    :param lb1, ub1, lb2, ub2: not batched
    :param accu_lb: accumulated LBs, batched
    :param accu_ub: accumulated UBs, batched
    :param eps: error bound epsilon, only diff larger than this are considered different. This is to handle numerical
                issues while boundary comparison. With 1e-6 it may get 4 pieces for network <1, 9>, while with 1e-7,
                it may get 70 pieces..
    :return: batched tensors
    """
    for i in range(len(lb1)):
        left_aligned = (lb1[i] - lb2[i]).abs() < eps
        right_aligned = (ub2[i] - ub1[i]).abs() < eps

        if len(lb1.shape) == 3:
            left_aligned = left_aligned.all()
            right_aligned = right_aligned.all()

        if left_aligned and right_aligned:
            continue

        if not left_aligned:
            # left piece
            if len(lb1[i].shape) == 3:
                assert (lb1[i] <= lb2[i]).all()
            else:
                assert lb1[i] < lb2[i]
            left_lb = lb1.clone()
            left_ub = ub1.clone()
            left_ub[i] = lb2[i]
            accu_lb = torch.cat((accu_lb, left_lb.unsqueeze(dim=0)), dim=0)
            accu_ub = torch.cat((accu_ub, left_ub.unsqueeze(dim=0)), dim=0)

        if not right_aligned:
            # right piece
            if len(ub2[i].shape) == 3:
                assert (ub2[i] <= ub1[i]).all()
            else:
                assert ub2[i] < ub1[i]
            right_lb = lb1.clone()
            right_ub = ub1.clone()
            right_lb[i] = ub2[i]
            accu_lb = torch.cat((accu_lb, right_lb.unsqueeze(dim=0)), dim=0)
            accu_ub = torch.cat((accu_ub, right_ub.unsqueeze(dim=0)), dim=0)

        lb1[i] = lb2[i]
        ub1[i] = ub2[i]
        return lbub_exclude(lb1, ub1, lb2, ub2, accu_lb, accu_ub)
    return accu_lb.to(device), accu_ub.to(device)
