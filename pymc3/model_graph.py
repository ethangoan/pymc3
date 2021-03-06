from collections import deque
from typing import Iterator, Optional, MutableSet

from theano.gof.graph import stack_search
from theano.compile import SharedVariable
from theano.tensor import Tensor

from .util import get_default_varnames
import pymc3 as pm

# this is a placeholder for a better characterization of the type
# of variables in a model.
RV = Tensor


class ModelGraph:
    def __init__(self, model):
        self.model = model
        self.var_names = get_default_varnames(self.model.named_vars, include_transformed=False)
        self.var_list = self.model.named_vars.values()
        self.transform_map = {v.transformed: v.name for v in self.var_list if hasattr(v, 'transformed')}
        self._deterministics = None

    def get_deterministics(self, var):
        """Compute the deterministic nodes of the graph, **not** including var itself."""
        deterministics = []
        attrs = ('transformed', 'logpt')
        for v in self.var_list:
            if v != var and all(not hasattr(v, attr) for attr in attrs):
                deterministics.append(v)
        return deterministics

    def _get_ancestors(self, var, func) -> MutableSet[RV]:
        """Get all ancestors of a function, doing some accounting for deterministics.
        """

        # this contains all of the variables in the model EXCEPT var...
        vars = set(self.var_list)
        vars.remove(var)

        blockers = set()
        retval = set()
        def _expand(node) -> Optional[Iterator[Tensor]]:
            if node in blockers:
                return None
            elif node in vars:
                blockers.add(node)
                retval.add(node)
                return None
            elif node.owner:
                blockers.add(node)
                return reversed(node.owner.inputs)
            else:
                return None

        stack_search(start = deque([func]),
                     expand=_expand,
                     mode='bfs')
        return retval

    def _filter_parents(self, var, parents):
        """Get direct parents of a var, as strings"""
        keep = set()
        for p in parents:
            if p == var:
                continue
            elif p.name in self.var_names:
                keep.add(p.name)
            elif p in self.transform_map:
                if self.transform_map[p] != var.name:
                    keep.add(self.transform_map[p])
            else:
                raise AssertionError('Do not know what to do with {}'.format(str(p)))
        return keep

    def get_parents(self, var):
        """Get the named nodes that are direct inputs to the var"""
        if hasattr(var, 'transformed'):
            func = var.transformed.logpt
        elif hasattr(var, 'logpt'):
            func = var.logpt
        else:
            func = var

        parents = self._get_ancestors(var, func)
        return self._filter_parents(var, parents)

    def make_compute_graph(self):
        """Get map of var_name -> set(input var names) for the model"""
        input_map = {}
        for var_name in self.var_names:
            input_map[var_name] = self.get_parents(self.model[var_name])
        return input_map

    def _make_node(self, var_name, graph):
        """Attaches the given variable to a graphviz Digraph"""
        v = self.model[var_name]

        # styling for node
        attrs = {}
        if isinstance(v, pm.model.ObservedRV):
            attrs['style'] = 'filled'

        if isinstance(v, SharedVariable):
            attrs['style'] = 'filled'

        # Get name for node
        if hasattr(v, 'distribution'):
            distribution = v.distribution.__class__.__name__
        else:
            distribution = 'Deterministic'
            attrs['shape'] = 'box'

        graph.node(var_name.replace(':', '&'),
                '{var_name} ~ {distribution}'.format(var_name=var_name, distribution=distribution),
                **attrs)

    def get_plates(self):
        """ Rough but surprisingly accurate plate detection.

        Just groups by the shape of the underlying distribution.  Will be wrong
        if there are two plates with the same shape.

        Returns
        -------
        dict: str -> set[str]
        """
        plates = {}
        for var_name in self.var_names:
            v = self.model[var_name]
            if hasattr(v, 'observations'):
                try:
                    # To get shape of _observed_ data container `pm.Data`
                    # (wrapper for theano.SharedVariable) we evaluate it.
                    shape = tuple(v.observations.shape.eval())
                except AttributeError:
                    shape = v.observations.shape
            elif hasattr(v, 'dshape'):
                shape = v.dshape
            else:
                shape = v.tag.test_value.shape
            if shape == (1,):
                shape = tuple()
            if shape not in plates:
                plates[shape] = set()
            plates[shape].add(var_name)
        return plates

    def make_graph(self):
        """Make graphviz Digraph of PyMC3 model

        Returns
        -------
        graphviz.Digraph
        """
        try:
            import graphviz
        except ImportError:
            raise ImportError('This function requires the python library graphviz, along with binaries. '
                              'The easiest way to install all of this is by running\n\n'
                              '\tconda install -c conda-forge python-graphviz')
        graph = graphviz.Digraph(self.model.name)
        for shape, var_names in self.get_plates().items():
            if isinstance(shape, SharedVariable):
                shape = shape.eval()
            label = ' x '.join(map('{:,d}'.format, shape))
            if label:
                # must be preceded by 'cluster' to get a box around it
                with graph.subgraph(name='cluster' + label) as sub:
                    for var_name in var_names:
                        self._make_node(var_name, sub)
                    # plate label goes bottom right
                    sub.attr(label=label, labeljust='r', labelloc='b', style='rounded')
            else:
                for var_name in var_names:
                    self._make_node(var_name, graph)

        for key, values in self.make_compute_graph().items():
            for value in values:
                graph.edge(value.replace(':', '&'), key.replace(':', '&'))
        return graph


def model_to_graphviz(model=None):
    """Produce a graphviz Digraph from a PyMC3 model.

    Requires graphviz, which may be installed most easily with
        conda install -c conda-forge python-graphviz

    Alternatively, you may install the `graphviz` binaries yourself,
    and then `pip install graphviz` to get the python bindings.  See
    http://graphviz.readthedocs.io/en/stable/manual.html
    for more information.
    """
    model = pm.modelcontext(model)
    return ModelGraph(model).make_graph()
