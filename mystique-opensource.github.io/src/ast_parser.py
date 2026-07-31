from typing import Generator

import tree_sitter_c as tsc
import tree_sitter_cpp as tscpp
import tree_sitter_java as tsjava
from tree_sitter import Language, Node, Parser

# tree-sitter >= 0.22 removed Language.query(); Query must be instantiated directly.
try:
    from tree_sitter import Query as _TSQuery
    def _build_query(language: Language, query_str: str):
        return _TSQuery(language, query_str)
except (ImportError, AttributeError):
    def _build_query(language: Language, query_str: str):
        return language.query(query_str)

import common

TS_JAVA_PACKAGE = "(package_declaration (scoped_identifier) @package)(package_declaration (identifier) @package)"
TS_JAVA_IMPORT = "(import_declaration (scoped_identifier) @import)"
TS_JAVA_CLASS = "(class_declaration) @class"
TS_JAVA_FIELD = "(field_declaration) @field"
TS_C_INCLUDE = "(preproc_include (system_lib_string)@string_content)(preproc_include (string_literal)@string_content)"
TS_C_METHOD = "(function_definition)@method"
TS_COND_STAT = "(if_statement)@name (while_statement)@name (for_statement)@name"
TS_ASSIGN_STAT = "(assignment_expression)@name"
TS_JAVA_METHOD = "(method_declaration) @method (constructor_declaration) @method"
TS_METHODNAME = "(method_declaration 	(identifier)@id)(constructor_declaration 	(identifier)@id)"
TS_FPARAM = "(formal_parameters)@name"


class ASTParser:
    def __init__(self, code: str | bytes, language: common.Language | int):
        if language == common.Language.C:
            self.LANGUAGE = Language(tsc.language())
        elif language == common.Language.CPP:
            self.LANGUAGE = Language(tscpp.language())
        elif language == common.Language.JAVA:
            self.LANGUAGE = Language(tsjava.language())
        else:
            self.LANGUAGE = Language(tsc.language())
        self.parser = Parser(self.LANGUAGE)
        if isinstance(code, str):
            self.tree = self.parser.parse(bytes(code, "utf-8"))
        elif isinstance(code, bytes):
            self.tree = self.parser.parse(code)
        self.root = self.tree.root_node

    @staticmethod
    def children_by_type_name(node: Node, type: str) -> list[Node]:
        node_list = []
        for child in node.named_children:
            if child.type == type:
                node_list.append(child)
        return node_list

    @staticmethod
    def child_by_type_name(node: Node, type: str) -> Node | None:
        for child in node.named_children:
            if child.type == type:
                return child
        return None

    def traverse_tree(self) -> Generator[Node, None, None]:
        cursor = self.tree.walk()
        visited_children = False
        while True:
            if not visited_children:
                assert cursor.node is not None
                yield cursor.node
                if not cursor.goto_first_child():
                    visited_children = True
            elif cursor.goto_next_sibling():
                visited_children = False
            elif not cursor.goto_parent():
                break

    @staticmethod
    def _iter_captures(captures) -> list[tuple[Node, str]]:
        """Normalise captures from both old (list of (Node, str)) and
        new (dict of str -> list[Node]) tree-sitter APIs."""
        if isinstance(captures, dict):
            nodes = []
            for capture_name, node_list in captures.items():
                for node in node_list:
                    nodes.append((node, capture_name))
            return nodes
        # Old API: list of (Node, capture_name) tuples
        return captures

    def query_oneshot(self, query_str: str) -> Node | None:
        query = _build_query(self.LANGUAGE, query_str)
        if hasattr(query, 'captures'):
            raw = query.captures(self.root)
        else:
            from tree_sitter import QueryCursor
            raw = QueryCursor(query).captures(self.root)
        nodes = self._iter_captures(raw)
        return nodes[0][0] if nodes else None

    def query(self, query_str: str):
        query = _build_query(self.LANGUAGE, query_str)
        if hasattr(query, 'captures'):
            raw = query.captures(self.root)
        else:
            from tree_sitter import QueryCursor
            raw = QueryCursor(query).captures(self.root)
        return self._iter_captures(raw)

    def query_from_node(self, node: Node, query_str: str):
        query = _build_query(self.LANGUAGE, query_str)
        if hasattr(query, 'captures'):
            raw = query.captures(node)
        else:
            from tree_sitter import QueryCursor
            raw = QueryCursor(query).captures(node)
        return self._iter_captures(raw)

    def get_error_nodes(self) -> list[Node]:
        query_str = """
        (ERROR)@error
        """
        return list(self.query(query_str))

    def get_all_identifier_node(self) -> list[Node]:
        query_str = """
        (identifier) @id
        """
        return list(self.query(query_str))

    def get_all_conditional_node(self) -> list[Node]:
        query_str = TS_COND_STAT
        return list(self.query(query_str))

    def get_all_assign_node(self) -> list[Node]:
        query_str = """
        (assignment_expression)@name  ( declaration )@name
        """
        return list(self.query(query_str))

    def get_all_return_node(self) -> list[Node]:
        query_str = """
        (return_statement)@name
        """
        return list(self.query(query_str))

    def get_all_call_node(self) -> list[Node]:
        query_str = """
        (call_expression)@name
        """
        return list(self.query(query_str))

    def get_all_includes(self) -> list[Node]:
        if self.LANGUAGE == Language(tscpp.language()) or self.LANGUAGE == Language(tsc.language()):
            query_str = """
            (preproc_include)@name
            """
        else:
            query_str = """
            ( import_declaration)@name
            """
        return list(self.query(query_str))
