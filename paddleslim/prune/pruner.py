# Copyright (c) 2019  PaddlePaddle Authors. All Rights Reserved.
#
# Licensed under the Apache License, Version 2.0 (the "License"
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.

import logging
import numpy as np
import paddle.fluid as fluid
import copy
from ..core import VarWrapper, OpWrapper, GraphWrapper
from ..common import get_logger

__all__ = ["Pruner"]

_logger = get_logger(__name__, level=logging.INFO)


class Pruner():
    def __init__(self, criterion="l1_norm"):
        """
        Args:
            criterion(str): the criterion used to sort channels for pruning.
                            It only supports 'l1_norm' currently.
        """
        self.criterion = criterion

    def prune(self,
              program,
              scope,
              params,
              ratios,
              place=None,
              lazy=False,
              only_graph=False,
              param_backup=False,
              param_shape_backup=False):
        """
        Pruning the given parameters.
        Args:
            program(fluid.Program): The program to be pruned.
            scope(fluid.Scope): The scope storing paramaters to be pruned.
            params(list<str>): A list of parameter names to be pruned.
            ratios(list<float>): A list of ratios to be used to pruning parameters.
            place(fluid.Place): The device place of filter parameters. Defalut: None.
            lazy(bool): True means setting the pruned elements to zero.
                        False means cutting down the pruned elements. Default: False.
            only_graph(bool): True means only modifying the graph.
                              False means modifying graph and variables in scope. Default: False.
            param_backup(bool): Whether to return a dict to backup the values of parameters. Default: False.
            param_shape_backup(bool): Whether to return a dict to backup the shapes of parameters. Default: False.
        Returns:
            Program: The pruned program.
            param_backup: A dict to backup the values of parameters.
            param_shape_backup: A dict to backup the shapes of parameters.
        """

        self.pruned_list = []
        graph = GraphWrapper(program.clone())
        param_backup = {} if param_backup else None
        param_shape_backup = {} if param_shape_backup else None
        self._prune_parameters(
            graph,
            scope,
            params,
            ratios,
            place,
            lazy=lazy,
            only_graph=only_graph,
            param_backup=param_backup,
            param_shape_backup=param_shape_backup)
        for op in graph.ops():
            if op.type() == 'depthwise_conv2d' or op.type(
            ) == 'depthwise_conv2d_grad':
                op.set_attr('groups', op.inputs('Filter')[0].shape()[0])
        return graph.program, param_backup, param_shape_backup

    def _prune_filters_by_ratio(self,
                                scope,
                                params,
                                ratio,
                                place,
                                lazy=False,
                                only_graph=False,
                                param_shape_backup=None,
                                param_backup=None):
        """
        Pruning filters by given ratio.
        Args:
            scope(fluid.core.Scope): The scope used to pruning filters.
            params(list<VarWrapper>): A list of filter parameters.
            ratio(float): The ratio to be pruned.
            place(fluid.Place): The device place of filter parameters.
            lazy(bool): True means setting the pruned elements to zero.
                        False means cutting down the pruned elements.
            only_graph(bool): True means only modifying the graph.
                              False means modifying graph and variables in  scope.
        """
        if params[0].name() in self.pruned_list[0]:
            return

        if only_graph:
            pruned_num = int(round(params[0].shape()[0] * ratio))
            for param in params:
                ori_shape = param.shape()
                if param_backup is not None and (
                        param.name() not in param_backup):
                    param_backup[param.name()] = copy.deepcopy(ori_shape)
                new_shape = list(ori_shape)
                new_shape[0] -= pruned_num
                param.set_shape(new_shape)
                _logger.debug("prune [{}] from {} to {}".format(param.name(
                ), ori_shape, new_shape))
                self.pruned_list[0].append(param.name())
            return range(pruned_num)

        else:

            param_t = scope.find_var(params[0].name()).get_tensor()
            pruned_idx = self._cal_pruned_idx(
                params[0].name(), np.array(param_t), ratio, axis=0)
            for param in params:
                assert isinstance(param, VarWrapper)
                param_t = scope.find_var(param.name()).get_tensor()
                if param_backup is not None and (
                        param.name() not in param_backup):
                    param_backup[param.name()] = copy.deepcopy(
                        np.array(param_t))
                try:
                    pruned_param = self._prune_tensor(
                        np.array(param_t),
                        pruned_idx,
                        pruned_axis=0,
                        lazy=lazy)
                except IndexError as e:
                    _logger.error("Pruning {}, but get [{}]".format(param.name(
                    ), e))

                param_t.set(pruned_param, place)
                ori_shape = param.shape()
                if param_shape_backup is not None and (
                        param.name() not in param_shape_backup):
                    param_shape_backup[param.name()] = copy.deepcopy(
                        param.shape())
                new_shape = list(param.shape())
                new_shape[0] = pruned_param.shape[0]
                param.set_shape(new_shape)
                _logger.debug("prune [{}] from {} to {}".format(param.name(
                ), ori_shape, new_shape))
                self.pruned_list[0].append(param.name())
            return pruned_idx

    def _prune_parameter_by_idx(self,
                                scope,
                                params,
                                pruned_idx,
                                pruned_axis,
                                place,
                                lazy=False,
                                only_graph=False,
                                param_shape_backup=None,
                                param_backup=None):
        """
        Pruning parameters in given axis.
        Args:
            scope(fluid.core.Scope): The scope storing paramaters to be pruned.
            params(VarWrapper): The parameter to be pruned.
            pruned_idx(list): The index of elements to be pruned.
            pruned_axis(int): The pruning axis.
            place(fluid.Place): The device place of filter parameters.
            lazy(bool): True means setting the pruned elements to zero.
                        False means cutting down the pruned elements.
            only_graph(bool): True means only modifying the graph.
                              False means modifying graph and variables in  scope.
        """
        if params[0].name() in self.pruned_list[pruned_axis]:
            return
        if only_graph:
            pruned_num = len(pruned_idx)
            for param in params:
                ori_shape = param.shape()
                if param_backup is not None and (
                        param.name() not in param_backup):
                    param_backup[param.name()] = copy.deepcopy(ori_shape)
                new_shape = list(ori_shape)
                new_shape[pruned_axis] -= pruned_num
                param.set_shape(new_shape)
                _logger.debug("prune [{}] from {} to {}".format(param.name(
                ), ori_shape, new_shape))
                self.pruned_list[pruned_axis].append(param.name())

        else:
            for param in params:
                assert isinstance(param, VarWrapper)
                param_t = scope.find_var(param.name()).get_tensor()
                if param_backup is not None and (
                        param.name() not in param_backup):
                    param_backup[param.name()] = copy.deepcopy(
                        np.array(param_t))
                pruned_param = self._prune_tensor(
                    np.array(param_t), pruned_idx, pruned_axis, lazy=lazy)
                param_t.set(pruned_param, place)
                ori_shape = param.shape()

                if param_shape_backup is not None and (
                        param.name() not in param_shape_backup):
                    param_shape_backup[param.name()] = copy.deepcopy(
                        param.shape())
                new_shape = list(param.shape())
                new_shape[pruned_axis] = pruned_param.shape[pruned_axis]
                param.set_shape(new_shape)
                _logger.debug("prune [{}] from {} to {}".format(param.name(
                ), ori_shape, new_shape))
                self.pruned_list[pruned_axis].append(param.name())

    def _forward_search_related_op(self, graph, node):
        """
        Forward search operators that will be affected by pruning of param.
        Args:
            graph(GraphWrapper): The graph to be searched.
            node(VarWrapper|OpWrapper): The current pruned parameter or operator.
        Returns:
            list<OpWrapper>: A list of operators.
        """
        visited = {}
        for op in graph.ops():
            visited[op.idx()] = False
        stack = []
        visit_path = []
        if isinstance(node, VarWrapper):
            for op in graph.ops():
                if (not op.is_bwd_op()) and (node in op.all_inputs()):
                    next_ops = self._get_next_unvisited_op(graph, visited, op)
                    #                visit_path.append(op)
                    visited[op.idx()] = True
                    for next_op in next_ops:
                        if visited[next_op.idx()] == False:
                            stack.append(next_op)
                            visit_path.append(next_op)
                            visited[next_op.idx()] = True
        elif isinstance(node, OpWrapper):
            next_ops = self._get_next_unvisited_op(graph, visited, node)
            for next_op in next_ops:
                if visited[next_op.idx()] == False:
                    stack.append(next_op)
                    visit_path.append(next_op)
                    visited[next_op.idx()] = True
        while len(stack) > 0:
            #top_op = stack[len(stack) - 1]
            top_op = stack.pop(0)
            next_ops = None
            if top_op.type() in ["conv2d", "deformable_conv"]:
                next_ops = None
            elif top_op.type() in ["mul", "concat"]:
                next_ops = None
            else:
                next_ops = self._get_next_unvisited_op(graph, visited, top_op)
            if next_ops != None:
                for op in next_ops:
                    if visited[op.idx()] == False:
                        stack.append(op)
                        visit_path.append(op)
                        visited[op.idx()] = True

        return visit_path

    def _get_next_unvisited_op(self, graph, visited, top_op):
        """
        Get next unvisited adjacent operators of given operators.
        Args:
            graph(GraphWrapper): The graph used to search. 
            visited(list): The ids of operators that has been visited.
            top_op: The given operator.
        Returns:
            list<OpWrapper>: A list of operators. 
        """
        assert isinstance(top_op, OpWrapper)
        next_ops = []
        for op in graph.next_ops(top_op):
            if (visited[op.idx()] == False) and (not op.is_bwd_op()):
                next_ops.append(op)
        return next_ops

    def _get_accumulator(self, graph, param):
        """
        Get accumulators of given parameter. The accumulator was created by optimizer.
        Args:
            graph(GraphWrapper): The graph used to search.
            param(VarWrapper): The given parameter.
        Returns:
            list<VarWrapper>: A list of accumulators which are variables.
        """
        assert isinstance(param, VarWrapper)
        params = []
        for op in param.outputs():
            if op.is_opt_op():
                for out_var in op.all_outputs():
                    if graph.is_persistable(out_var) and out_var.name(
                    ) != param.name():
                        params.append(out_var)
        return params

    def _forward_pruning_ralated_params(self,
                                        graph,
                                        scope,
                                        param,
                                        place,
                                        ratio=None,
                                        pruned_idxs=None,
                                        lazy=False,
                                        only_graph=False,
                                        param_backup=None,
                                        param_shape_backup=None):
        """
        Pruning all the parameters affected by the pruning of given parameter.
        Args:
            graph(GraphWrapper): The graph to be searched.
            scope(fluid.core.Scope): The scope storing paramaters to be pruned.
            param(VarWrapper): The given parameter.
            place(fluid.Place): The device place of filter parameters.
            ratio(float): The target ratio to be pruned.
            pruned_idx(list): The index of elements to be pruned.
            lazy(bool): True means setting the pruned elements to zero.
                        False means cutting down the pruned elements.
            only_graph(bool): True means only modifying the graph.
                              False means modifying graph and variables in  scope.
        """
        assert isinstance(
            graph,
            GraphWrapper), "graph must be instance of slim.core.GraphWrapper"
        assert isinstance(
            param,
            VarWrapper), "param must be instance of slim.core.VarWrapper"

        if param.name() in self.pruned_list[0]:
            return
        related_ops = self._forward_search_related_op(graph, param)
        for op in related_ops:
            _logger.debug("relate op: {};".format(op))
        if ratio is None:
            assert pruned_idxs is not None
            self._prune_parameter_by_idx(
                scope, [param] + self._get_accumulator(graph, param),
                pruned_idxs,
                pruned_axis=0,
                place=place,
                lazy=lazy,
                only_graph=only_graph,
                param_backup=param_backup,
                param_shape_backup=param_shape_backup)

        else:
            pruned_idxs = self._prune_filters_by_ratio(
                scope, [param] + self._get_accumulator(graph, param),
                ratio,
                place,
                lazy=lazy,
                only_graph=only_graph,
                param_backup=param_backup,
                param_shape_backup=param_shape_backup)
        self._prune_ops(related_ops, pruned_idxs, graph, scope, place, lazy,
                        only_graph, param_backup, param_shape_backup)

    def _prune_ops(self, ops, pruned_idxs, graph, scope, place, lazy,
                   only_graph, param_backup, param_shape_backup):
        for idx, op in enumerate(ops):
            if op.type() in ["conv2d", "deformable_conv"]:
                for in_var in op.all_inputs():
                    if graph.is_parameter(in_var):
                        conv_param = in_var
                        self._prune_parameter_by_idx(
                            scope, [conv_param] + self._get_accumulator(
                                graph, conv_param),
                            pruned_idxs,
                            pruned_axis=1,
                            place=place,
                            lazy=lazy,
                            only_graph=only_graph,
                            param_backup=param_backup,
                            param_shape_backup=param_shape_backup)
            if op.type() == "depthwise_conv2d":
                for in_var in op.all_inputs():
                    if graph.is_parameter(in_var):
                        conv_param = in_var
                        self._prune_parameter_by_idx(
                            scope, [conv_param] + self._get_accumulator(
                                graph, conv_param),
                            pruned_idxs,
                            pruned_axis=0,
                            place=place,
                            lazy=lazy,
                            only_graph=only_graph,
                            param_backup=param_backup,
                            param_shape_backup=param_shape_backup)
            elif op.type() == "elementwise_add":
                # pruning bias
                for in_var in op.all_inputs():
                    if graph.is_parameter(in_var):
                        bias_param = in_var
                        self._prune_parameter_by_idx(
                            scope, [bias_param] + self._get_accumulator(
                                graph, bias_param),
                            pruned_idxs,
                            pruned_axis=0,
                            place=place,
                            lazy=lazy,
                            only_graph=only_graph,
                            param_backup=param_backup,
                            param_shape_backup=param_shape_backup)
            elif op.type() == "mul":  # pruning fc layer
                fc_input = None
                fc_param = None
                for in_var in op.all_inputs():
                    if graph.is_parameter(in_var):
                        fc_param = in_var
                    else:
                        fc_input = in_var

                idx = []
                feature_map_size = fc_input.shape()[2] * fc_input.shape()[3]
                range_idx = np.array(range(feature_map_size))
                for i in pruned_idxs:
                    idx += list(range_idx + i * feature_map_size)
                corrected_idxs = idx
                self._prune_parameter_by_idx(
                    scope, [fc_param] + self._get_accumulator(graph, fc_param),
                    corrected_idxs,
                    pruned_axis=0,
                    place=place,
                    lazy=lazy,
                    only_graph=only_graph,
                    param_backup=param_backup,
                    param_shape_backup=param_shape_backup)

            elif op.type() == "concat":
                concat_inputs = op.all_inputs()
                last_op = ops[idx - 1]
                concat_idx = None
                for last_op in reversed(ops):
                    for out_var in last_op.all_outputs():
                        if out_var in concat_inputs:
                            concat_idx = concat_inputs.index(out_var)
                            break
                    if concat_idx is not None:
                        break
                offset = 0
                for ci in range(concat_idx):
                    offset += concat_inputs[ci].shape()[1]
                corrected_idxs = [x + offset for x in pruned_idxs]
                related_ops = self._forward_search_related_op(graph, op)

                for op in related_ops:
                    _logger.debug("concat relate op: {};".format(op))

                self._prune_ops(related_ops, corrected_idxs, graph, scope,
                                place, lazy, only_graph, param_backup,
                                param_shape_backup)
            elif op.type() == "batch_norm":
                bn_inputs = op.all_inputs()
                in_num = len(bn_inputs)
                beta = bn_inputs[0]
                mean = bn_inputs[1]
                alpha = bn_inputs[2]
                variance = bn_inputs[3]
                self._prune_parameter_by_idx(
                    scope, [mean] + self._get_accumulator(graph, mean),
                    pruned_idxs,
                    pruned_axis=0,
                    place=place,
                    lazy=lazy,
                    only_graph=only_graph,
                    param_backup=param_backup,
                    param_shape_backup=param_shape_backup)
                self._prune_parameter_by_idx(
                    scope, [variance] + self._get_accumulator(graph, variance),
                    pruned_idxs,
                    pruned_axis=0,
                    place=place,
                    lazy=lazy,
                    only_graph=only_graph,
                    param_backup=param_backup,
                    param_shape_backup=param_shape_backup)
                self._prune_parameter_by_idx(
                    scope, [alpha] + self._get_accumulator(graph, alpha),
                    pruned_idxs,
                    pruned_axis=0,
                    place=place,
                    lazy=lazy,
                    only_graph=only_graph,
                    param_backup=param_backup,
                    param_shape_backup=param_shape_backup)
                self._prune_parameter_by_idx(
                    scope, [beta] + self._get_accumulator(graph, beta),
                    pruned_idxs,
                    pruned_axis=0,
                    place=place,
                    lazy=lazy,
                    only_graph=only_graph,
                    param_backup=param_backup,
                    param_shape_backup=param_shape_backup)

    def _prune_parameters(self,
                          graph,
                          scope,
                          params,
                          ratios,
                          place,
                          lazy=False,
                          only_graph=False,
                          param_backup=None,
                          param_shape_backup=None):
        """
        Pruning the given parameters.
        Args:
            graph(GraphWrapper): The graph to be searched.
            scope(fluid.core.Scope): The scope storing paramaters to be pruned.
            params(list<str>): A list of parameter names to be pruned.
            ratios(list<float>): A list of ratios to be used to pruning parameters.
            place(fluid.Place): The device place of filter parameters.
            pruned_idx(list): The index of elements to be pruned.
            lazy(bool): True means setting the pruned elements to zero.
                        False means cutting down the pruned elements.
            only_graph(bool): True means only modifying the graph.
                              False means modifying graph and variables in  scope.
        """
        assert len(params) == len(ratios)
        self.pruned_list = [[], []]
        for param, ratio in zip(params, ratios):
            assert isinstance(param, str) or isinstance(param, unicode)
            if param in self.pruned_list[0]:
                _logger.info("Skip {}".format(param))
                continue
            _logger.info("pruning param: {}".format(param))
            param = graph.var(param)
            self._forward_pruning_ralated_params(
                graph,
                scope,
                param,
                place,
                ratio=ratio,
                lazy=lazy,
                only_graph=only_graph,
                param_backup=param_backup,
                param_shape_backup=param_shape_backup)
            ops = param.outputs()
            for op in ops:
                if op.type() in ['conv2d', 'deformable_conv']:
                    brother_ops = self._search_brother_ops(graph, op)
                    for broher in brother_ops:
                        _logger.debug("pruning brother: {}".format(broher))
                        for p in graph.get_param_by_op(broher):
                            self._forward_pruning_ralated_params(
                                graph,
                                scope,
                                p,
                                place,
                                ratio=ratio,
                                lazy=lazy,
                                only_graph=only_graph,
                                param_backup=param_backup,
                                param_shape_backup=param_shape_backup)

    def _search_brother_ops(self, graph, op_node):
        """
        Search brother operators that was affected by pruning of given operator.
        Args:
            graph(GraphWrapper): The graph to be searched.
            op_node(OpWrapper): The start node for searching.
        Returns: 
            list<VarWrapper>: A list of operators.
        """
        _logger.debug("######################search: {}######################".
                      format(op_node))
        visited = [op_node.idx()]
        stack = []
        brothers = []
        for op in graph.next_ops(op_node):
            if ("conv2d" not in op.type()) and (
                    "concat" not in op.type()) and (
                        "deformable_conv" not in op.type()) and (
                            op.type() != 'fc') and (
                                not op.is_bwd_op()) and (not op.is_opt_op()):
                stack.append(op)
                visited.append(op.idx())
        while len(stack) > 0:
            top_op = stack.pop()
            for parent in graph.pre_ops(top_op):
                if parent.idx() not in visited and (
                        not parent.is_bwd_op()) and (not parent.is_opt_op()):
                    _logger.debug("----------go back from {} to {}----------".
                                  format(top_op, parent))
                    if (('conv2d' in parent.type()) or
                        ("deformable_conv" in parent.type()) or
                        (parent.type() == 'fc')):
                        brothers.append(parent)
                    else:
                        stack.append(parent)
                    visited.append(parent.idx())

            for child in graph.next_ops(top_op):
                if ('conv2d' not in child.type()) and (
                        "concat" not in child.type()) and (
                            'deformable_conv' not in child.type()) and (
                                child.type() != 'fc') and (
                                    child.idx() not in visited) and (
                                        not child.is_bwd_op()) and (
                                            not child.is_opt_op()):
                    stack.append(child)
                    visited.append(child.idx())
        _logger.debug("brothers: {}".format(brothers))
        _logger.debug(
            "######################Finish search######################".format(
                op_node))
        return brothers

    def _cal_pruned_idx(self, name, param, ratio, axis):
        """
        Calculate the index to be pruned on axis by given pruning ratio.
        Args:
            name(str): The name of parameter to be pruned.
            param(np.array): The data of parameter to be pruned.
            ratio(float): The ratio to be pruned.
            axis(int): The axis to be used for pruning given parameter.
                       If it is None, the value in self.pruning_axis will be used.
                       default: None.
        Returns:
            list<int>: The indexes to be pruned on axis.
        """
        prune_num = int(round(param.shape[axis] * ratio))
        reduce_dims = [i for i in range(len(param.shape)) if i != axis]
        if self.criterion == 'l1_norm':
            criterions = np.sum(np.abs(param), axis=tuple(reduce_dims))
        pruned_idx = criterions.argsort()[:prune_num]
        return pruned_idx

    def _prune_tensor(self, tensor, pruned_idx, pruned_axis, lazy=False):
        """
        Pruning a array by indexes on given axis.
        Args:
            tensor(numpy.array): The target array to be pruned.
            pruned_idx(list<int>): The indexes to be pruned.
            pruned_axis(int): The axis of given array to be pruned on. 
            lazy(bool): True means setting the pruned elements to zero.
                        False means remove the pruned elements from memory.
                        default: False.
        Returns:
            numpy.array: The pruned array.
        """
        mask = np.zeros(tensor.shape[pruned_axis], dtype=bool)
        mask[pruned_idx] = True

        def func(data):
            return data[~mask]

        def lazy_func(data):
            data[mask] = 0
            return data

        if lazy:
            return np.apply_along_axis(lazy_func, pruned_axis, tensor)
        else:
            return np.apply_along_axis(func, pruned_axis, tensor)
