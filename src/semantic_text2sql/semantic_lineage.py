"""Conservative expression lineage. Unsupported scopes return None, never proof.

This is AST inspection only: no SQL execution, model calls, or algebraic guesses.
"""
from __future__ import annotations

from typing import Any, cast

from sqlglot import exp
from sqlglot.optimizer.scope import Scope, build_scope


class ExpressionLineage:
    def __init__(self, root: exp.Expression, context: dict[str, Any] | None = None) -> None:
        self.root = build_scope(root)
        self.context = (context or {}).get("tables", {})
        self.remaining_nodes = 4096

    def resolve(
        self, expression: exp.Expression, scope: Scope | None = None,
        *, aliases: bool = False, depth: int = 0,
    ) -> exp.Expression | None:
        scope = scope or self.root
        if (scope is None or depth > 24 or self.remaining_nodes <= 0
                or not isinstance(scope.expression, exp.Select)):
            return None
        self.remaining_nodes -= 1
        if isinstance(expression, exp.Alias):
            return self.resolve(expression.this, scope, depth=depth + 1)
        if isinstance(expression, exp.Paren):
            return self.resolve(expression.this, scope, aliases=aliases, depth=depth + 1)
        if isinstance(expression, exp.Query):
            return None
        if isinstance(expression, exp.Column):
            if expression.this.args.get("quoted"):
                return None  # Case-sensitive quoted lineage needs dialect-specific evidence.
            if aliases and not expression.table:
                matches = [item for item in scope.expression.expressions
                           if isinstance(item, exp.Alias)
                           and item.alias.casefold() == expression.name.casefold()]
                if len(matches) == 1:
                    return self.resolve(matches[0].this, scope, depth=depth + 1)
            sources = [(name, source) for name, (_, source) in scope.selected_sources.items()
                       if not expression.table or name.casefold() == expression.table.casefold()]
            if not expression.table and len(sources) > 1:
                sources = [(name, source) for name, source in sources
                           if isinstance(source, exp.Table)
                           and expression.name in self.context.get(
                               source.name, {}).get("columns", {})]
            if len(sources) != 1:
                return None
            source = sources[0][1]
            if isinstance(source, Scope):
                if not isinstance(source.expression, exp.Select):
                    return None
                projections = [item for item in source.expression.expressions
                               if item.alias_or_name.casefold() == expression.name.casefold()]
                if len(projections) != 1 or isinstance(projections[0], exp.Star):
                    return None
                return self.resolve(projections[0].unalias(), source, depth=depth + 1)
            if not isinstance(source, exp.Table):
                return None
            if any(part.args.get("quoted") for part in source.parts):
                return None
            # Keep full physical identity; CTE aliases are never physical tables.
            relation = ".".join(part.name for part in source.parts)
            return exp.column(expression.name.casefold(), table=relation.casefold())
        result = expression.copy()
        for key, value in expression.args.items():
            if isinstance(value, exp.Expression):
                resolved = self.resolve(value, scope, depth=depth + 1)
                if resolved is None:
                    return None
                result.set(key, resolved)
            elif isinstance(value, list):
                resolved_values = []
                for item in value:
                    if isinstance(item, exp.Expression):
                        resolved_item = self.resolve(item, scope, depth=depth + 1)
                        if resolved_item is None:
                            return None
                        resolved_values.append(resolved_item)
                    else:
                        resolved_values.append(item)
                result.set(key, resolved_values)
        return result

    def output_expressions(self) -> list[exp.Expression | None]:
        if self.root is None or not isinstance(self.root.expression, exp.Select):
            return []
        return [self.resolve(item) for item in self.root.expression.expressions]

    def reachable(self) -> list[Scope]:
        """Referenced FROM/CTE scopes only; unused CTEs cannot prove a requirement."""
        visited: set[int] = set()
        result: list[Scope] = []

        def visit(scope: Scope) -> None:
            if id(scope) in visited:
                return
            visited.add(id(scope))
            result.append(scope)
            for _, source in scope.selected_sources.values():
                if isinstance(source, Scope):
                    visit(source)

        if self.root is not None:
            visit(self.root)
        return result


def expression_key(expression: exp.Expression, *, qualified: bool = True) -> str:
    """Normalize identifiers/parens only; do not erase casts or date operations."""
    def normalize(node: exp.Expression) -> exp.Expression:
        if isinstance(node, exp.Paren):
            return cast(exp.Expression, node.this)
        if isinstance(node, exp.Identifier):
            return exp.to_identifier(node.name.casefold())
        if isinstance(node, exp.Column):
            relation = ".".join(part for part in (node.catalog, node.db, node.table) if part)
            return exp.column(node.name.casefold(),
                              table=relation.casefold() if qualified else None)
        return node

    return expression.transform(normalize).sql()
