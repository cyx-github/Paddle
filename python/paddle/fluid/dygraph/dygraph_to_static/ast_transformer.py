#   Copyright (c) 2020 PaddlePaddle Authors. All Rights Reserved.
#
# Licensed under the Apache License, Version 2.0 (the "License");
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

from __future__ import print_function

import astor
import copy
# gast is a generic AST to represent Python2 and Python3's Abstract Syntax Tree(AST).
# It provides a compatibility layer between the AST of various Python versions,
# as produced by ast.parse from the standard ast module.
# See details in https://github.com/serge-sans-paille/gast/
import gast
import inspect
import textwrap

from paddle.fluid import unique_name

from paddle.fluid.dygraph.dygraph_to_static.break_continue_transformer import BreakContinueTransformer
from paddle.fluid.dygraph.dygraph_to_static.ifelse_transformer import IfElseTransformer
from paddle.fluid.dygraph.dygraph_to_static.list_transformer import ListTransformer
from paddle.fluid.dygraph.dygraph_to_static.loop_transformer import LoopTransformer
from paddle.fluid.dygraph.dygraph_to_static.tensor_shape_transformer import TensorShapeTransformer

from paddle.fluid.dygraph.dygraph_to_static.static_analysis import AstNodeWrapper
from paddle.fluid.dygraph.dygraph_to_static.static_analysis import NodeVarType
from paddle.fluid.dygraph.dygraph_to_static.static_analysis import StaticAnalysisVisitor
from paddle.fluid.dygraph.dygraph_to_static.utils import ast_to_func
from paddle.fluid.dygraph.dygraph_to_static.utils import is_paddle_api, is_dygraph_api, is_to_variable
from paddle.fluid.dygraph.dygraph_to_static.utils import to_assign_node, to_static_ast, update_args_of_func
from paddle.fluid.dygraph.dygraph_to_static.utils import dygraph_class_to_static_api

__all__ = ['DygraphToStaticAst', 'convert_to_static']

DECORATOR_NAMES = [
    'dygraph_to_static_code', 'dygraph_to_static_program',
    'dygraph_to_static_func', 'dygraph_to_static_output'
]


class DygraphToStaticAst(gast.NodeTransformer):
    """
    Main class to transform Dygraph to Static Graph
    """

    def get_static_ast(self, root):
        # save root for some analysis may need global AST
        self.root = root
        self.static_analysis_visitor = StaticAnalysisVisitor(root)
        self.static_analysis_root = self.static_analysis_visitor.get_node_wrapper_root(
        )

        self.decorate_func_name = None
        self.arg_name_to_idx = {}
        self.transfer_from_node_type(self.static_analysis_root)
        return self.static_analysis_root

    def transfer_from_node_type(self, node_wrapper):
        # Generic transformation
        self.visit(node_wrapper.node)

        # Transform basic api of dygraph to static graph and get feed_name_to_arg_name
        basic_api_trans = BasicApiTransformer(node_wrapper)
        basic_api_trans.transform()
        self.feed_name_to_arg_name = basic_api_trans.get_feed_name_to_arg_id()

        # Transform Tensor.shape into fluid.layers.shape(Tensor)
        TensorShapeTransformer(node_wrapper).transform()

        # Transform list used in control flow
        ListTransformer(node_wrapper).transform()

        # Transform break/continue in loops
        BreakContinueTransformer(node_wrapper).transform()

        # Transform for loop and while loop
        LoopTransformer(node_wrapper).transform()

        # Transform all if/else statement of Dygraph into Static Graph.
        IfElseTransformer(node_wrapper).transform()

    def visit_FunctionDef(self, node):
        if self.decorate_func_name is None:
            self.decorate_func_name = node.name
            for idx, arg in enumerate(node.args.args):
                self.arg_name_to_idx[arg.id] = idx

        self.generic_visit(node)
        # Remove the decorated name of dygraph_to_static
        if hasattr(node, 'decorator_list'):
            decorator_list = [
                d for d in node.decorator_list if d.id not in DECORATOR_NAMES
            ]
            node.decorator_list = decorator_list
        return node

    def get_module_name(self):
        """
        Return the main function name which will be used as module name
        in ast_to_func.
        """
        # Should consider BaseAPITransformer which add new module name in Yamei's PR.
        assert self.decorate_func_name, "decorate_func_name shall not be None."
        return self.decorate_func_name

    def get_feed_name_to_idx(self):
        feed_name_to_idx = {}
        for feed_name, arg_name in self.feed_name_to_arg_name.items():
            feed_name_to_idx[feed_name] = self.arg_name_to_idx.get(arg_name)
        return feed_name_to_idx


class BasicApiTransformer(gast.NodeTransformer):
    """
    Class to transform basic API from dygraph to static graph.
    """

    def __init__(self, wrapper_root):
        assert isinstance(
            wrapper_root, AstNodeWrapper
        ), "Input non-AstNodeWrapper node for the initialization of BasicApiTransformer."

        self.wrapper_root = wrapper_root
        self.root = wrapper_root.node
        self.class_node_dict = {}

        # Used for transformation of data feed
        self.feed_name_to_arg_id = {}
        self.name_to_tensor_shape = {}

    def transform(self):
        self.visit(self.root)
        return self.wrapper_root

    def visit_FunctionDef(self, node):
        self.generic_visit(node)
        if hasattr(node, 'decorator_list'):
            decorator_list = [
                d for d in node.decorator_list if d.id not in DECORATOR_NAMES
            ]
            node.decorator_list = decorator_list
        return node

    def visit_Assign(self, node):
        if self._update_class_node_dict(node):
            return None

        for child_node in gast.walk(node.value):
            if isinstance(child_node, gast.Call):
                self._visit_Call(child_node)
        return node

    def visit_Expr(self, node):
        value_node = node.value
        for child_node in gast.walk(value_node):
            if isinstance(child_node, gast.Call):
                if is_dygraph_api(child_node):
                    return
                else:
                    self._visit_Call(child_node)
        return node

    def _visit_Call(self, node):
        assert isinstance(node, gast.Call)
        # Replace API `to_variable` with `fluid.layers.assign`
        if is_to_variable(node):
            self._update_feed_dict(node)
            node = to_assign_node(node)
            return node

        func_name = astor.to_source(gast.gast_to_ast(node.func))

        if self._is_dygraph_forward(func_name):
            class_node = self._get_class_node(func_name)
            static_node = to_static_ast(node, class_node)
            return static_node
        else:
            return node

    def _is_dygraph_forward(self, func_id):
        return func_id in self.class_node_dict

    def _get_class_node(self, func_id):
        return self.class_node_dict[func_id]

    def _update_class_node_dict(self, node):
        assert isinstance(node, gast.Assign)
        node_value = node.value
        if isinstance(node_value, gast.Call):
            if is_to_variable(node_value):
                return False

            if is_dygraph_api(node_value):
                dygraph_api = node_value.func.attr
                if not dygraph_class_to_static_api.get(dygraph_api):
                    return False

                update_args_of_func(node_value, node_value, "__init__")
                target_str = astor.to_source(gast.gast_to_ast(node.targets[0]))
                self.class_node_dict[target_str] = node_value
                return True
            # TODO: node.value is not dygraph class
        return False

    def _update_feed_dict(self, node):
        assert isinstance(node, gast.Call)

        value_node = None
        for kw in node.keywords:
            if kw.arg == 'value':
                value_node = kw.value  # eg: `a` for "value=a "
        if not value_node:
            value_node = node.args[0]

        if not isinstance(value_node, gast.Name):
            return
        else:
            var_name = value_node.id
            feed_var_name = unique_name.generate(var_name)  # eg: "a_0"
            self.feed_name_to_arg_id[
                feed_var_name] = var_name  # eg: "a_0" : "a"

    def get_feed_name_to_arg_id(self):
        return self.feed_name_to_arg_id


def convert_to_static(dyfunc):
    """
    Converts dygraph function into static function.
    """
    # Get AST from dygraph function
    raw_code = inspect.getsource(dyfunc)
    code = textwrap.dedent(raw_code)
    root = gast.parse(code)

    # Transform AST
    dygraph_to_static = DygraphToStaticAst()
    root_wrapper = dygraph_to_static.get_static_ast(root)

    # Get static_func from AST
    static_func, file_name = ast_to_func(root_wrapper.node, dyfunc)
    return static_func, dygraph_to_static
