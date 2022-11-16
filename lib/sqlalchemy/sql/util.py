# sql/util.py
# Copyright (C) 2005-2022 the SQLAlchemy authors and contributors
# <see AUTHORS file>
#
# This module is part of SQLAlchemy and is released under
# the MIT License: https://www.opensource.org/licenses/mit-license.php
# mypy: allow-untyped-defs, allow-untyped-calls

"""High level utilities which build upon other modules here.

"""
from __future__ import annotations

from collections import deque
from itertools import chain
import typing
from typing import AbstractSet
from typing import Any
from typing import Callable
from typing import cast
from typing import Collection
from typing import Dict
from typing import Iterable
from typing import Iterator
from typing import List
from typing import Optional
from typing import overload
from typing import Sequence
from typing import Tuple
from typing import TYPE_CHECKING
from typing import TypeVar
from typing import Union

from . import coercions
from . import operators
from . import roles
from . import visitors
from ._typing import is_text_clause
from .annotation import _deep_annotate as _deep_annotate  # noqa: F401
from .annotation import _deep_deannotate as _deep_deannotate  # noqa: F401
from .annotation import _shallow_annotate as _shallow_annotate  # noqa: F401
from .base import _expand_cloned
from .base import _from_objects
from .cache_key import HasCacheKey as HasCacheKey  # noqa: F401
from .ddl import sort_tables as sort_tables  # noqa: F401
from .elements import _find_columns as _find_columns
from .elements import _label_reference
from .elements import _textual_label_reference
from .elements import BindParameter
from .elements import ClauseElement
from .elements import ColumnClause
from .elements import ColumnElement
from .elements import Grouping
from .elements import KeyedColumnElement
from .elements import Label
from .elements import Null
from .elements import UnaryExpression
from .schema import Column
from .selectable import Alias
from .selectable import FromClause
from .selectable import FromGrouping
from .selectable import Join
from .selectable import ScalarSelect
from .selectable import SelectBase
from .selectable import TableClause
from .visitors import _ET
from .. import exc
from .. import util
from ..util.typing import Literal
from ..util.typing import Protocol

if typing.TYPE_CHECKING:
    from ._typing import _ColumnExpressionArgument
    from ._typing import _EquivalentColumnMap
    from ._typing import _TypeEngineArgument
    from .elements import BinaryExpression
    from .elements import TextClause
    from .selectable import _JoinTargetElement
    from .selectable import _SelectIterable
    from .selectable import Selectable
    from .visitors import _TraverseCallableType
    from .visitors import ExternallyTraversible
    from .visitors import ExternalTraversal
    from ..engine.interfaces import _AnyExecuteParams
    from ..engine.interfaces import _AnyMultiExecuteParams
    from ..engine.interfaces import _AnySingleExecuteParams
    from ..engine.interfaces import _CoreSingleExecuteParams
    from ..engine.row import Row

_CE = TypeVar("_CE", bound="ColumnElement[Any]")


def join_condition(
    a: FromClause,
    b: FromClause,
    a_subset: Optional[FromClause] = None,
    consider_as_foreign_keys: Optional[AbstractSet[ColumnClause[Any]]] = None,
) -> ColumnElement[bool]:
    """Create a join condition between two tables or selectables.

    e.g.::

        join_condition(tablea, tableb)

    would produce an expression along the lines of::

        tablea.c.id==tableb.c.tablea_id

    The join is determined based on the foreign key relationships
    between the two selectables.   If there are multiple ways
    to join, or no way to join, an error is raised.

    :param a_subset: An optional expression that is a sub-component
        of ``a``.  An attempt will be made to join to just this sub-component
        first before looking at the full ``a`` construct, and if found
        will be successful even if there are other ways to join to ``a``.
        This allows the "right side" of a join to be passed thereby
        providing a "natural join".

    """
    return Join._join_condition(
        a,
        b,
        a_subset=a_subset,
        consider_as_foreign_keys=consider_as_foreign_keys,
    )


def find_join_source(
    clauses: List[FromClause], join_to: FromClause
) -> List[int]:
    """Given a list of FROM clauses and a selectable,
    return the first index and element from the list of
    clauses which can be joined against the selectable.  returns
    None, None if no match is found.

    e.g.::

        clause1 = table1.join(table2)
        clause2 = table4.join(table5)

        join_to = table2.join(table3)

        find_join_source([clause1, clause2], join_to) == clause1

    """

    selectables = list(_from_objects(join_to))
    idx = []
    for i, f in enumerate(clauses):
        for s in selectables:
            if f.is_derived_from(s):
                idx.append(i)
    return idx


def find_left_clause_that_matches_given(
    clauses: Sequence[FromClause], join_from: FromClause
) -> List[int]:
    """Given a list of FROM clauses and a selectable,
    return the indexes from the list of
    clauses which is derived from the selectable.

    """

    selectables = list(_from_objects(join_from))
    liberal_idx = []
    for i, f in enumerate(clauses):
        for s in selectables:
            # basic check, if f is derived from s.
            # this can be joins containing a table, or an aliased table
            # or select statement matching to a table.  This check
            # will match a table to a selectable that is adapted from
            # that table.  With Query, this suits the case where a join
            # is being made to an adapted entity
            if f.is_derived_from(s):
                liberal_idx.append(i)
                break

    # in an extremely small set of use cases, a join is being made where
    # there are multiple FROM clauses where our target table is represented
    # in more than one, such as embedded or similar.   in this case, do
    # another pass where we try to get a more exact match where we aren't
    # looking at adaption relationships.
    if len(liberal_idx) > 1:
        conservative_idx = []
        for idx in liberal_idx:
            f = clauses[idx]
            for s in selectables:
                if set(surface_selectables(f)).intersection(
                    surface_selectables(s)
                ):
                    conservative_idx.append(idx)
                    break
        if conservative_idx:
            return conservative_idx

    return liberal_idx


def find_left_clause_to_join_from(
    clauses: Sequence[FromClause],
    join_to: _JoinTargetElement,
    onclause: Optional[ColumnElement[Any]],
) -> List[int]:
    """Given a list of FROM clauses, a selectable,
    and optional ON clause, return a list of integer indexes from the
    clauses list indicating the clauses that can be joined from.

    The presence of an "onclause" indicates that at least one clause can
    definitely be joined from; if the list of clauses is of length one
    and the onclause is given, returns that index.   If the list of clauses
    is more than length one, and the onclause is given, attempts to locate
    which clauses contain the same columns.

    """
    idx = []
    selectables = set(_from_objects(join_to))

    # if we are given more than one target clause to join
    # from, use the onclause to provide a more specific answer.
    # otherwise, don't try to limit, after all, "ON TRUE" is a valid
    # on clause
    if len(clauses) > 1 and onclause is not None:
        resolve_ambiguity = True
        cols_in_onclause = _find_columns(onclause)
    else:
        resolve_ambiguity = False
        cols_in_onclause = None

    for i, f in enumerate(clauses):
        for s in selectables.difference([f]):
            if resolve_ambiguity:
                assert cols_in_onclause is not None
                if set(f.c).union(s.c).issuperset(cols_in_onclause):
                    idx.append(i)
                    break
            elif onclause is not None or Join._can_join(f, s):
                idx.append(i)
                break

    if len(idx) > 1:
        # this is the same "hide froms" logic from
        # Selectable._get_display_froms
        toremove = set(
            chain(*[_expand_cloned(f._hide_froms) for f in clauses])
        )
        idx = [i for i in idx if clauses[i] not in toremove]

    # onclause was given and none of them resolved, so assume
    # all indexes can match
    if not idx and onclause is not None:
        return list(range(len(clauses)))
    else:
        return idx


def visit_binary_product(
    fn: Callable[
        [BinaryExpression[Any], ColumnElement[Any], ColumnElement[Any]], None
    ],
    expr: ColumnElement[Any],
) -> None:
    """Produce a traversal of the given expression, delivering
    column comparisons to the given function.

    The function is of the form::

        def my_fn(binary, left, right)

    For each binary expression located which has a
    comparison operator, the product of "left" and
    "right" will be delivered to that function,
    in terms of that binary.

    Hence an expression like::

        and_(
            (a + b) == q + func.sum(e + f),
            j == r
        )

    would have the traversal::

        a <eq> q
        a <eq> e
        a <eq> f
        b <eq> q
        b <eq> e
        b <eq> f
        j <eq> r

    That is, every combination of "left" and
    "right" that doesn't further contain
    a binary comparison is passed as pairs.

    """
    stack: List[BinaryExpression[Any]] = []

    def visit(element: ClauseElement) -> Iterator[ColumnElement[Any]]:
        if isinstance(element, ScalarSelect):
            # we don't want to dig into correlated subqueries,
            # those are just column elements by themselves
            yield element
        elif element.__visit_name__ == "binary" and operators.is_comparison(
            element.operator  # type: ignore
        ):
            stack.insert(0, element)  # type: ignore
            for l in visit(element.left):  # type: ignore
                for r in visit(element.right):  # type: ignore
                    fn(stack[0], l, r)
            stack.pop(0)
            for elem in element.get_children():
                visit(elem)
        else:
            if isinstance(element, ColumnClause):
                yield element
            for elem in element.get_children():
                for e in visit(elem):
                    yield e

    list(visit(expr))
    visit = None  # type: ignore  # remove gc cycles


def find_tables(
    clause: ClauseElement,
    *,
    check_columns: bool = False,
    include_aliases: bool = False,
    include_joins: bool = False,
    include_selects: bool = False,
    include_crud: bool = False,
) -> List[TableClause]:
    """locate Table objects within the given expression."""

    tables: List[TableClause] = []
    _visitors: Dict[str, _TraverseCallableType[Any]] = {}

    if include_selects:
        _visitors["select"] = _visitors["compound_select"] = tables.append

    if include_joins:
        _visitors["join"] = tables.append

    if include_aliases:
        _visitors["alias"] = _visitors["subquery"] = _visitors[
            "tablesample"
        ] = _visitors["lateral"] = tables.append

    if include_crud:
        _visitors["insert"] = _visitors["update"] = _visitors[
            "delete"
        ] = lambda ent: tables.append(ent.table)

    if check_columns:

        def visit_column(column):
            tables.append(column.table)

        _visitors["column"] = visit_column

    _visitors["table"] = tables.append

    visitors.traverse(clause, {}, _visitors)
    return tables


def unwrap_order_by(clause):
    """Break up an 'order by' expression into individual column-expressions,
    without DESC/ASC/NULLS FIRST/NULLS LAST"""

    cols = util.column_set()
    result = []
    stack = deque([clause])

    # examples
    # column -> ASC/DESC == column
    # column -> ASC/DESC -> label == column
    # column -> label -> ASC/DESC -> label == column
    # scalar_select -> label -> ASC/DESC == scalar_select -> label

    while stack:
        t = stack.popleft()
        if isinstance(t, ColumnElement) and (
            not isinstance(t, UnaryExpression)
            or not operators.is_ordering_modifier(t.modifier)  # type: ignore
        ):
            if isinstance(t, Label) and not isinstance(
                t.element, ScalarSelect
            ):
                t = t.element

                if isinstance(t, Grouping):
                    t = t.element

                stack.append(t)
                continue
            elif isinstance(t, _label_reference):
                t = t.element

                stack.append(t)
                continue
            if isinstance(t, (_textual_label_reference)):
                continue
            if t not in cols:
                cols.add(t)
                result.append(t)

        else:
            for c in t.get_children():
                stack.append(c)
    return result


def unwrap_label_reference(element):
    def replace(
        element: ExternallyTraversible, **kw: Any
    ) -> Optional[ExternallyTraversible]:
        if isinstance(element, _label_reference):
            return element.element
        elif isinstance(element, _textual_label_reference):
            assert False, "can't unwrap a textual label reference"
        return None

    return visitors.replacement_traverse(element, {}, replace)


def expand_column_list_from_order_by(collist, order_by):
    """Given the columns clause and ORDER BY of a selectable,
    return a list of column expressions that can be added to the collist
    corresponding to the ORDER BY, without repeating those already
    in the collist.

    """
    cols_already_present = set(
        [
            col.element if col._order_by_label_element is not None else col
            for col in collist
        ]
    )

    to_look_for = list(chain(*[unwrap_order_by(o) for o in order_by]))

    return [col for col in to_look_for if col not in cols_already_present]


def clause_is_present(clause, search):
    """Given a target clause and a second to search within, return True
    if the target is plainly present in the search without any
    subqueries or aliases involved.

    Basically descends through Joins.

    """

    for elem in surface_selectables(search):
        if clause == elem:  # use == here so that Annotated's compare
            return True
    else:
        return False


def tables_from_leftmost(clause: FromClause) -> Iterator[FromClause]:
    if isinstance(clause, Join):
        for t in tables_from_leftmost(clause.left):
            yield t
        for t in tables_from_leftmost(clause.right):
            yield t
    elif isinstance(clause, FromGrouping):
        for t in tables_from_leftmost(clause.element):
            yield t
    else:
        yield clause


def surface_selectables(clause):
    stack = [clause]
    while stack:
        elem = stack.pop()
        yield elem
        if isinstance(elem, Join):
            stack.extend((elem.left, elem.right))
        elif isinstance(elem, FromGrouping):
            stack.append(elem.element)


def surface_selectables_only(clause):
    stack = [clause]
    while stack:
        elem = stack.pop()
        if isinstance(elem, (TableClause, Alias)):
            yield elem
        if isinstance(elem, Join):
            stack.extend((elem.left, elem.right))
        elif isinstance(elem, FromGrouping):
            stack.append(elem.element)
        elif isinstance(elem, ColumnClause):
            if elem.table is not None:
                stack.append(elem.table)
            else:
                yield elem
        elif elem is not None:
            yield elem


def extract_first_column_annotation(column, annotation_name):
    filter_ = (FromGrouping, SelectBase)

    stack = deque([column])
    while stack:
        elem = stack.popleft()
        if annotation_name in elem._annotations:
            return elem._annotations[annotation_name]
        for sub in elem.get_children():
            if isinstance(sub, filter_):
                continue
            stack.append(sub)
    return None


def selectables_overlap(left: FromClause, right: FromClause) -> bool:
    """Return True if left/right have some overlapping selectable"""

    return bool(
        set(surface_selectables(left)).intersection(surface_selectables(right))
    )


def bind_values(clause):
    """Return an ordered list of "bound" values in the given clause.

    E.g.::

        >>> expr = and_(
        ...    table.c.foo==5, table.c.foo==7
        ... )
        >>> bind_values(expr)
        [5, 7]
    """

    v = []

    def visit_bindparam(bind):
        v.append(bind.effective_value)

    visitors.traverse(clause, {}, {"bindparam": visit_bindparam})
    return v


def _quote_ddl_expr(element):
    if isinstance(element, str):
        element = element.replace("'", "''")
        return "'%s'" % element
    else:
        return repr(element)


class _repr_base:
    _LIST: int = 0
    _TUPLE: int = 1
    _DICT: int = 2

    __slots__ = ("max_chars",)

    max_chars: int

    def trunc(self, value: Any) -> str:
        rep = repr(value)
        lenrep = len(rep)
        if lenrep > self.max_chars:
            segment_length = self.max_chars // 2
            rep = (
                rep[0:segment_length]
                + (
                    " ... (%d characters truncated) ... "
                    % (lenrep - self.max_chars)
                )
                + rep[-segment_length:]
            )
        return rep


def _repr_single_value(value):
    rp = _repr_base()
    rp.max_chars = 300
    return rp.trunc(value)


class _repr_row(_repr_base):
    """Provide a string view of a row."""

    __slots__ = ("row",)

    def __init__(self, row: "Row[Any]", max_chars: int = 300):
        self.row = row
        self.max_chars = max_chars

    def __repr__(self) -> str:
        trunc = self.trunc
        return "(%s%s)" % (
            ", ".join(trunc(value) for value in self.row),
            "," if len(self.row) == 1 else "",
        )


class _long_statement(str):
    def __str__(self) -> str:
        lself = len(self)
        if lself > 500:
            lleft = 250
            lright = 100
            trunc = lself - lleft - lright
            return (
                f"{self[0:lleft]} ... {trunc} "
                f"characters truncated ... {self[-lright:]}"
            )
        else:
            return str.__str__(self)


class _repr_params(_repr_base):
    """Provide a string view of bound parameters.

    Truncates display to a given number of 'multi' parameter sets,
    as well as long values to a given number of characters.

    """

    __slots__ = "params", "batches", "ismulti", "max_params"

    def __init__(
        self,
        params: Optional[_AnyExecuteParams],
        batches: int,
        max_params: int = 100,
        max_chars: int = 300,
        ismulti: Optional[bool] = None,
    ):
        self.params = params
        self.ismulti = ismulti
        self.batches = batches
        self.max_chars = max_chars
        self.max_params = max_params

    def __repr__(self) -> str:
        if self.ismulti is None:
            return self.trunc(self.params)

        if isinstance(self.params, list):
            typ = self._LIST

        elif isinstance(self.params, tuple):
            typ = self._TUPLE
        elif isinstance(self.params, dict):
            typ = self._DICT
        else:
            return self.trunc(self.params)

        if self.ismulti:
            multi_params = cast(
                "_AnyMultiExecuteParams",
                self.params,
            )

            if len(self.params) > self.batches:
                msg = (
                    " ... displaying %i of %i total bound parameter sets ... "
                )
                return " ".join(
                    (
                        self._repr_multi(
                            multi_params[: self.batches - 2],
                            typ,
                        )[0:-1],
                        msg % (self.batches, len(self.params)),
                        self._repr_multi(multi_params[-2:], typ)[1:],
                    )
                )
            else:
                return self._repr_multi(multi_params, typ)
        else:
            return self._repr_params(
                cast(
                    "_AnySingleExecuteParams",
                    self.params,
                ),
                typ,
            )

    def _repr_multi(
        self,
        multi_params: _AnyMultiExecuteParams,
        typ: int,
    ) -> str:
        if multi_params:
            if isinstance(multi_params[0], list):
                elem_type = self._LIST
            elif isinstance(multi_params[0], tuple):
                elem_type = self._TUPLE
            elif isinstance(multi_params[0], dict):
                elem_type = self._DICT
            else:
                assert False, "Unknown parameter type %s" % (
                    type(multi_params[0])
                )

            elements = ", ".join(
                self._repr_params(params, elem_type) for params in multi_params
            )
        else:
            elements = ""

        if typ == self._LIST:
            return "[%s]" % elements
        else:
            return "(%s)" % elements

    def _get_batches(self, params: Iterable[Any]) -> Any:

        lparams = list(params)
        lenparams = len(lparams)
        if lenparams > self.max_params:
            lleft = self.max_params // 2
            return (
                lparams[0:lleft],
                lparams[-lleft:],
                lenparams - self.max_params,
            )
        else:
            return lparams, None, None

    def _repr_params(
        self,
        params: _AnySingleExecuteParams,
        typ: int,
    ) -> str:
        if typ is self._DICT:
            return self._repr_param_dict(
                cast("_CoreSingleExecuteParams", params)
            )
        elif typ is self._TUPLE:
            return self._repr_param_tuple(cast("Sequence[Any]", params))
        else:
            return self._repr_param_list(params)

    def _repr_param_dict(self, params: _CoreSingleExecuteParams) -> str:
        trunc = self.trunc
        (
            items_first_batch,
            items_second_batch,
            trunclen,
        ) = self._get_batches(params.items())

        if items_second_batch:
            text = "{%s" % (
                ", ".join(
                    f"{key!r}: {trunc(value)}"
                    for key, value in items_first_batch
                )
            )
            text += f" ... {trunclen} parameters truncated ... "
            text += "%s}" % (
                ", ".join(
                    f"{key!r}: {trunc(value)}"
                    for key, value in items_second_batch
                )
            )
        else:
            text = "{%s}" % (
                ", ".join(
                    f"{key!r}: {trunc(value)}"
                    for key, value in items_first_batch
                )
            )
        return text

    def _repr_param_tuple(self, params: "Sequence[Any]") -> str:
        trunc = self.trunc

        (
            items_first_batch,
            items_second_batch,
            trunclen,
        ) = self._get_batches(params)

        if items_second_batch:
            text = "(%s" % (
                ", ".join(trunc(value) for value in items_first_batch)
            )
            text += f" ... {trunclen} parameters truncated ... "
            text += "%s)" % (
                ", ".join(trunc(value) for value in items_second_batch),
            )
        else:
            text = "(%s%s)" % (
                ", ".join(trunc(value) for value in items_first_batch),
                "," if len(items_first_batch) == 1 else "",
            )
        return text

    def _repr_param_list(self, params: _AnySingleExecuteParams) -> str:
        trunc = self.trunc
        (
            items_first_batch,
            items_second_batch,
            trunclen,
        ) = self._get_batches(params)

        if items_second_batch:
            text = "[%s" % (
                ", ".join(trunc(value) for value in items_first_batch)
            )
            text += f" ... {trunclen} parameters truncated ... "
            text += "%s]" % (
                ", ".join(trunc(value) for value in items_second_batch)
            )
        else:
            text = "[%s]" % (
                ", ".join(trunc(value) for value in items_first_batch)
            )
        return text


def adapt_criterion_to_null(crit: _CE, nulls: Collection[Any]) -> _CE:
    """given criterion containing bind params, convert selected elements
    to IS NULL.

    """

    def visit_binary(binary):
        if (
            isinstance(binary.left, BindParameter)
            and binary.left._identifying_key in nulls
        ):
            # reverse order if the NULL is on the left side
            binary.left = binary.right
            binary.right = Null()
            binary.operator = operators.is_
            binary.negate = operators.is_not
        elif (
            isinstance(binary.right, BindParameter)
            and binary.right._identifying_key in nulls
        ):
            binary.right = Null()
            binary.operator = operators.is_
            binary.negate = operators.is_not

    return visitors.cloned_traverse(crit, {}, {"binary": visit_binary})


def splice_joins(
    left: Optional[FromClause],
    right: Optional[FromClause],
    stop_on: Optional[FromClause] = None,
) -> Optional[FromClause]:
    if left is None:
        return right

    stack: List[Tuple[Optional[FromClause], Optional[Join]]] = [(right, None)]

    adapter = ClauseAdapter(left)
    ret = None
    while stack:
        (right, prevright) = stack.pop()
        if isinstance(right, Join) and right is not stop_on:
            right = right._clone()
            right.onclause = adapter.traverse(right.onclause)
            stack.append((right.left, right))
        else:
            right = adapter.traverse(right)
        if prevright is not None:
            assert right is not None
            prevright.left = right
        if ret is None:
            ret = right

    return ret


@overload
def reduce_columns(
    columns: Iterable[ColumnElement[Any]],
    *clauses: Optional[ClauseElement],
    **kw: bool,
) -> Sequence[ColumnElement[Any]]:
    ...


@overload
def reduce_columns(
    columns: _SelectIterable,
    *clauses: Optional[ClauseElement],
    **kw: bool,
) -> Sequence[Union[ColumnElement[Any], TextClause]]:
    ...


def reduce_columns(
    columns: _SelectIterable,
    *clauses: Optional[ClauseElement],
    **kw: bool,
) -> Collection[Union[ColumnElement[Any], TextClause]]:
    r"""given a list of columns, return a 'reduced' set based on natural
    equivalents.

    the set is reduced to the smallest list of columns which have no natural
    equivalent present in the list.  A "natural equivalent" means that two
    columns will ultimately represent the same value because they are related
    by a foreign key.

    \*clauses is an optional list of join clauses which will be traversed
    to further identify columns that are "equivalent".

    \**kw may specify 'ignore_nonexistent_tables' to ignore foreign keys
    whose tables are not yet configured, or columns that aren't yet present.

    This function is primarily used to determine the most minimal "primary
    key" from a selectable, by reducing the set of primary key columns present
    in the selectable to just those that are not repeated.

    """
    ignore_nonexistent_tables = kw.pop("ignore_nonexistent_tables", False)
    only_synonyms = kw.pop("only_synonyms", False)

    column_set = util.OrderedSet(columns)
    cset_no_text: util.OrderedSet[ColumnElement[Any]] = column_set.difference(
        c for c in column_set if is_text_clause(c)  # type: ignore
    )

    omit = util.column_set()
    for col in cset_no_text:
        for fk in chain(*[c.foreign_keys for c in col.proxy_set]):
            for c in cset_no_text:
                if c is col:
                    continue
                try:
                    fk_col = fk.column
                except exc.NoReferencedColumnError:
                    # TODO: add specific coverage here
                    # to test/sql/test_selectable ReduceTest
                    if ignore_nonexistent_tables:
                        continue
                    else:
                        raise
                except exc.NoReferencedTableError:
                    # TODO: add specific coverage here
                    # to test/sql/test_selectable ReduceTest
                    if ignore_nonexistent_tables:
                        continue
                    else:
                        raise
                if fk_col.shares_lineage(c) and (
                    not only_synonyms or c.name == col.name
                ):
                    omit.add(col)
                    break

    if clauses:

        def visit_binary(binary):
            if binary.operator == operators.eq:
                cols = util.column_set(
                    chain(
                        *[c.proxy_set for c in cset_no_text.difference(omit)]
                    )
                )
                if binary.left in cols and binary.right in cols:
                    for c in reversed(cset_no_text):
                        if c.shares_lineage(binary.right) and (
                            not only_synonyms or c.name == binary.left.name
                        ):
                            omit.add(c)
                            break

        for clause in clauses:
            if clause is not None:
                visitors.traverse(clause, {}, {"binary": visit_binary})

    return column_set.difference(omit)


def criterion_as_pairs(
    expression,
    consider_as_foreign_keys=None,
    consider_as_referenced_keys=None,
    any_operator=False,
):
    """traverse an expression and locate binary criterion pairs."""

    if consider_as_foreign_keys and consider_as_referenced_keys:
        raise exc.ArgumentError(
            "Can only specify one of "
            "'consider_as_foreign_keys' or "
            "'consider_as_referenced_keys'"
        )

    def col_is(a, b):
        # return a is b
        return a.compare(b)

    def visit_binary(binary):
        if not any_operator and binary.operator is not operators.eq:
            return
        if not isinstance(binary.left, ColumnElement) or not isinstance(
            binary.right, ColumnElement
        ):
            return

        if consider_as_foreign_keys:
            if binary.left in consider_as_foreign_keys and (
                col_is(binary.right, binary.left)
                or binary.right not in consider_as_foreign_keys
            ):
                pairs.append((binary.right, binary.left))
            elif binary.right in consider_as_foreign_keys and (
                col_is(binary.left, binary.right)
                or binary.left not in consider_as_foreign_keys
            ):
                pairs.append((binary.left, binary.right))
        elif consider_as_referenced_keys:
            if binary.left in consider_as_referenced_keys and (
                col_is(binary.right, binary.left)
                or binary.right not in consider_as_referenced_keys
            ):
                pairs.append((binary.left, binary.right))
            elif binary.right in consider_as_referenced_keys and (
                col_is(binary.left, binary.right)
                or binary.left not in consider_as_referenced_keys
            ):
                pairs.append((binary.right, binary.left))
        else:
            if isinstance(binary.left, Column) and isinstance(
                binary.right, Column
            ):
                if binary.left.references(binary.right):
                    pairs.append((binary.right, binary.left))
                elif binary.right.references(binary.left):
                    pairs.append((binary.left, binary.right))

    pairs: List[Tuple[ColumnElement[Any], ColumnElement[Any]]] = []
    visitors.traverse(expression, {}, {"binary": visit_binary})
    return pairs


class ClauseAdapter(visitors.ReplacingExternalTraversal):
    """Clones and modifies clauses based on column correspondence.

    E.g.::

      table1 = Table('sometable', metadata,
          Column('col1', Integer),
          Column('col2', Integer)
          )
      table2 = Table('someothertable', metadata,
          Column('col1', Integer),
          Column('col2', Integer)
          )

      condition = table1.c.col1 == table2.c.col1

    make an alias of table1::

      s = table1.alias('foo')

    calling ``ClauseAdapter(s).traverse(condition)`` converts
    condition to read::

      s.c.col1 == table2.c.col1

    """

    def __init__(
        self,
        selectable: Selectable,
        equivalents: Optional[_EquivalentColumnMap] = None,
        include_fn: Optional[Callable[[ClauseElement], bool]] = None,
        exclude_fn: Optional[Callable[[ClauseElement], bool]] = None,
        adapt_on_names: bool = False,
        anonymize_labels: bool = False,
        adapt_from_selectables: Optional[AbstractSet[FromClause]] = None,
    ):
        self.__traverse_options__ = {
            "stop_on": [selectable],
            "anonymize_labels": anonymize_labels,
        }
        self.selectable = selectable
        self.include_fn = include_fn
        self.exclude_fn = exclude_fn
        self.equivalents = util.column_dict(equivalents or {})
        self.adapt_on_names = adapt_on_names
        self.adapt_from_selectables = adapt_from_selectables

    if TYPE_CHECKING:

        @overload
        def traverse(self, obj: Literal[None]) -> None:
            ...

        # note this specializes the ReplacingExternalTraversal.traverse()
        # method to state
        # that we will return the same kind of ExternalTraversal object as
        # we were given.  This is probably not 100% true, such as it's
        # possible for us to swap out Alias for Table at the top level.
        # Ideally there could be overloads specific to ColumnElement and
        # FromClause but Mypy is not accepting those as compatible with
        # the base ReplacingExternalTraversal
        @overload
        def traverse(self, obj: _ET) -> _ET:
            ...

        def traverse(
            self, obj: Optional[ExternallyTraversible]
        ) -> Optional[ExternallyTraversible]:
            ...

    def _corresponding_column(
        self, col, require_embedded, _seen=util.EMPTY_SET
    ):

        newcol = self.selectable.corresponding_column(
            col, require_embedded=require_embedded
        )
        if newcol is None and col in self.equivalents and col not in _seen:
            for equiv in self.equivalents[col]:
                newcol = self._corresponding_column(
                    equiv,
                    require_embedded=require_embedded,
                    _seen=_seen.union([col]),
                )
                if newcol is not None:
                    return newcol
        if self.adapt_on_names and newcol is None:
            newcol = self.selectable.exported_columns.get(col.name)
        return newcol

    @util.preload_module("sqlalchemy.sql.functions")
    def replace(
        self, col: _ET, _include_singleton_constants: bool = False
    ) -> Optional[_ET]:
        functions = util.preloaded.sql_functions

        # TODO: cython candidate

        if isinstance(col, FromClause) and not isinstance(
            col, functions.FunctionElement
        ):

            if self.selectable.is_derived_from(col):
                if self.adapt_from_selectables:
                    for adp in self.adapt_from_selectables:
                        if adp.is_derived_from(col):
                            break
                    else:
                        return None
                return self.selectable  # type: ignore
            elif isinstance(col, Alias) and isinstance(
                col.element, TableClause
            ):
                # we are a SELECT statement and not derived from an alias of a
                # table (which nonetheless may be a table our SELECT derives
                # from), so return the alias to prevent further traversal
                # or
                # we are an alias of a table and we are not derived from an
                # alias of a table (which nonetheless may be the same table
                # as ours) so, same thing
                return col  # type: ignore
            else:
                # other cases where we are a selectable and the element
                # is another join or selectable that contains a table which our
                # selectable derives from, that we want to process
                return None

        elif not isinstance(col, ColumnElement):
            return None
        elif not _include_singleton_constants and col._is_singleton_constant:
            # dont swap out NULL, TRUE, FALSE for a label name
            # in a SQL statement that's being rewritten,
            # leave them as the constant.  This is first noted in #6259,
            # however the logic to check this moved here as of #7154 so that
            # it is made specific to SQL rewriting and not all column
            # correspondence
            return None

        if "adapt_column" in col._annotations:
            col = col._annotations["adapt_column"]

        if TYPE_CHECKING:
            assert isinstance(col, KeyedColumnElement)

        if self.adapt_from_selectables and col not in self.equivalents:
            for adp in self.adapt_from_selectables:
                if adp.c.corresponding_column(col, False) is not None:
                    break
            else:
                return None

        if TYPE_CHECKING:
            assert isinstance(col, KeyedColumnElement)

        if self.include_fn and not self.include_fn(col):
            return None
        elif self.exclude_fn and self.exclude_fn(col):
            return None
        else:
            return self._corresponding_column(  # type: ignore
                col, require_embedded=True
            )


class _ColumnLookup(Protocol):
    @overload
    def __getitem__(self, key: None) -> None:
        ...

    @overload
    def __getitem__(self, key: ColumnClause[Any]) -> ColumnClause[Any]:
        ...

    @overload
    def __getitem__(self, key: ColumnElement[Any]) -> ColumnElement[Any]:
        ...

    @overload
    def __getitem__(self, key: _ET) -> _ET:
        ...

    def __getitem__(self, key: Any) -> Any:
        ...


class ColumnAdapter(ClauseAdapter):
    """Extends ClauseAdapter with extra utility functions.

    Key aspects of ColumnAdapter include:

    * Expressions that are adapted are stored in a persistent
      .columns collection; so that an expression E adapted into
      an expression E1, will return the same object E1 when adapted
      a second time.   This is important in particular for things like
      Label objects that are anonymized, so that the ColumnAdapter can
      be used to present a consistent "adapted" view of things.

    * Exclusion of items from the persistent collection based on
      include/exclude rules, but also independent of hash identity.
      This because "annotated" items all have the same hash identity as their
      parent.

    * "wrapping" capability is added, so that the replacement of an expression
      E can proceed through a series of adapters.  This differs from the
      visitor's "chaining" feature in that the resulting object is passed
      through all replacing functions unconditionally, rather than stopping
      at the first one that returns non-None.

    * An adapt_required option, used by eager loading to indicate that
      We don't trust a result row column that is not translated.
      This is to prevent a column from being interpreted as that
      of the child row in a self-referential scenario, see
      inheritance/test_basic.py->EagerTargetingTest.test_adapt_stringency

    """

    columns: _ColumnLookup

    def __init__(
        self,
        selectable: Selectable,
        equivalents: Optional[_EquivalentColumnMap] = None,
        adapt_required: bool = False,
        include_fn: Optional[Callable[[ClauseElement], bool]] = None,
        exclude_fn: Optional[Callable[[ClauseElement], bool]] = None,
        adapt_on_names: bool = False,
        allow_label_resolve: bool = True,
        anonymize_labels: bool = False,
        adapt_from_selectables: Optional[AbstractSet[FromClause]] = None,
    ):
        ClauseAdapter.__init__(
            self,
            selectable,
            equivalents,
            include_fn=include_fn,
            exclude_fn=exclude_fn,
            adapt_on_names=adapt_on_names,
            anonymize_labels=anonymize_labels,
            adapt_from_selectables=adapt_from_selectables,
        )

        self.columns = util.WeakPopulateDict(self._locate_col)  # type: ignore
        if self.include_fn or self.exclude_fn:
            self.columns = self._IncludeExcludeMapping(self, self.columns)
        self.adapt_required = adapt_required
        self.allow_label_resolve = allow_label_resolve
        self._wrap = None

    class _IncludeExcludeMapping:
        def __init__(self, parent, columns):
            self.parent = parent
            self.columns = columns

        def __getitem__(self, key):
            if (
                self.parent.include_fn and not self.parent.include_fn(key)
            ) or (self.parent.exclude_fn and self.parent.exclude_fn(key)):
                if self.parent._wrap:
                    return self.parent._wrap.columns[key]
                else:
                    return key
            return self.columns[key]

    def wrap(self, adapter):
        ac = self.__class__.__new__(self.__class__)
        ac.__dict__.update(self.__dict__)
        ac._wrap = adapter
        ac.columns = util.WeakPopulateDict(ac._locate_col)  # type: ignore
        if ac.include_fn or ac.exclude_fn:
            ac.columns = self._IncludeExcludeMapping(ac, ac.columns)

        return ac

    @overload
    def traverse(self, obj: Literal[None]) -> None:
        ...

    @overload
    def traverse(self, obj: _ET) -> _ET:
        ...

    def traverse(
        self, obj: Optional[ExternallyTraversible]
    ) -> Optional[ExternallyTraversible]:
        return self.columns[obj]

    def chain(self, visitor: ExternalTraversal) -> ColumnAdapter:
        assert isinstance(visitor, ColumnAdapter)

        return super().chain(visitor)

    if TYPE_CHECKING:

        @property
        def visitor_iterator(self) -> Iterator[ColumnAdapter]:
            ...

    adapt_clause = traverse
    adapt_list = ClauseAdapter.copy_and_process

    def adapt_check_present(
        self, col: ColumnElement[Any]
    ) -> Optional[ColumnElement[Any]]:
        newcol = self.columns[col]

        if newcol is col and self._corresponding_column(col, True) is None:
            return None

        return newcol

    def _locate_col(
        self, col: ColumnElement[Any]
    ) -> Optional[ColumnElement[Any]]:
        # both replace and traverse() are overly complicated for what
        # we are doing here and we would do better to have an inlined
        # version that doesn't build up as much overhead.  the issue is that
        # sometimes the lookup does in fact have to adapt the insides of
        # say a labeled scalar subquery.   However, if the object is an
        # Immutable, i.e. Column objects, we can skip the "clone" /
        # "copy internals" part since those will be no-ops in any case.
        # additionally we want to catch singleton objects null/true/false
        # and make sure they are adapted as well here.

        if col._is_immutable:
            for vis in self.visitor_iterator:
                c = vis.replace(col, _include_singleton_constants=True)
                if c is not None:
                    break
            else:
                c = col
        else:
            c = ClauseAdapter.traverse(self, col)

        if self._wrap:
            c2 = self._wrap._locate_col(c)
            if c2 is not None:
                c = c2

        if self.adapt_required and c is col:
            return None

        # allow_label_resolve is consumed by one case for joined eager loading
        # as part of its logic to prevent its own columns from being affected
        # by .order_by().  Before full typing were applied to the ORM, this
        # logic would set this attribute on the incoming object (which is
        # typically a column, but we have a test for it being a non-column
        # object) if no column were found.  While this seemed to
        # have no negative effects, this adjustment should only occur on the
        # new column which is assumed to be local to an adapted selectable.
        if c is not col:
            c._allow_label_resolve = self.allow_label_resolve

        return c

    def __getstate__(self):
        d = self.__dict__.copy()
        del d["columns"]
        return d

    def __setstate__(self, state):
        self.__dict__.update(state)
        self.columns = util.WeakPopulateDict(self._locate_col)  # type: ignore


def _offset_or_limit_clause(
    element: Union[int, _ColumnExpressionArgument[int]],
    name: Optional[str] = None,
    type_: Optional[_TypeEngineArgument[int]] = None,
) -> ColumnElement[int]:
    """Convert the given value to an "offset or limit" clause.

    This handles incoming integers and converts to an expression; if
    an expression is already given, it is passed through.

    """
    return coercions.expect(
        roles.LimitOffsetRole, element, name=name, type_=type_
    )


def _offset_or_limit_clause_asint_if_possible(
    clause: Optional[Union[int, _ColumnExpressionArgument[int]]]
) -> Optional[Union[int, _ColumnExpressionArgument[int]]]:
    """Return the offset or limit clause as a simple integer if possible,
    else return the clause.

    """
    if clause is None:
        return None
    if hasattr(clause, "_limit_offset_value"):
        value = clause._limit_offset_value  # type: ignore
        return util.asint(value)
    else:
        return clause


def _make_slice(
    limit_clause: Optional[Union[int, _ColumnExpressionArgument[int]]],
    offset_clause: Optional[Union[int, _ColumnExpressionArgument[int]]],
    start: int,
    stop: int,
) -> Tuple[Optional[ColumnElement[int]], Optional[ColumnElement[int]]]:
    """Compute LIMIT/OFFSET in terms of slice start/end"""

    # for calculated limit/offset, try to do the addition of
    # values to offset in Python, however if a SQL clause is present
    # then the addition has to be on the SQL side.

    # TODO: typing is finding a few gaps in here, see if they can be
    # closed up

    if start is not None and stop is not None:
        offset_clause = _offset_or_limit_clause_asint_if_possible(
            offset_clause
        )
        if offset_clause is None:
            offset_clause = 0

        if start != 0:
            offset_clause = offset_clause + start  # type: ignore

        if offset_clause == 0:
            offset_clause = None
        else:
            assert offset_clause is not None
            offset_clause = _offset_or_limit_clause(offset_clause)

        limit_clause = _offset_or_limit_clause(stop - start)

    elif start is None and stop is not None:
        limit_clause = _offset_or_limit_clause(stop)
    elif start is not None and stop is None:
        offset_clause = _offset_or_limit_clause_asint_if_possible(
            offset_clause
        )
        if offset_clause is None:
            offset_clause = 0

        if start != 0:
            offset_clause = offset_clause + start  # type: ignore

        if offset_clause == 0:
            offset_clause = None
        else:
            offset_clause = _offset_or_limit_clause(
                offset_clause  # type: ignore
            )

    return limit_clause, offset_clause  # type: ignore
