from typing import Any, Dict, List, Optional

from app.domain.command import CommandArgumentConfig, ExecutionContext


class PipelineBuilder:
    """Pure, I/O-free assembly of the final command pipeline from a context.

    Produces ``List[List[str]]`` (e.g. ``[["ls", "-al"], ["grep", "ssh"]]``)
    with no side-effects, making it trivially unit-testable in isolation.
    """

    def resolve_part(
        self,
        part: str,
        arguments: Dict[str, Any],
        arg_defs: List[CommandArgumentConfig],
        run_id: Optional[str] = None,
    ) -> str:
        """Replace {placeholder} tokens in a single command part.

        User-argument placeholders come from ``arguments``/``arg_defs``.
        ``{run_id}`` is server-injected (never a user argument).
        """
        for arg in arg_defs:
            placeholder = f"{{{arg.name}}}"
            if placeholder in part:
                part = part.replace(placeholder, str(arguments[arg.name]))
        if run_id is not None and "{run_id}" in part:
            part = part.replace("{run_id}", run_id)
        return part

    def strip_omitted_optionals(
        self,
        command: List[str],
        arguments: Dict[str, Any],
        arg_defs: List[CommandArgumentConfig],
    ) -> List[str]:
        """Remove pipeline tokens for optional args that weren't supplied.

        For each optional (``required=False``) arg the request omitted, drop the
        token containing its ``{name}`` placeholder AND the flag token directly
        before it (so ``["--limit", "{limit}"]`` disappears entirely).
        """
        omitted = {
            arg.name
            for arg in arg_defs
            if not arg.required and arguments.get(arg.name) is None
        }
        if not omitted:
            return command
        omitted_placeholders = {f"{{{name}}}" for name in omitted}

        drop = set()
        for i, tok in enumerate(command):
            if any(ph in tok for ph in omitted_placeholders):
                drop.add(i)
                if (
                    i > 0
                    and command[i - 1].startswith("-")
                    and "{" not in command[i - 1]
                ):
                    drop.add(i - 1)
        return [tok for i, tok in enumerate(command) if i not in drop]

    def build(self, context: ExecutionContext) -> List[List[str]]:
        """Resolve all {placeholder} tokens and return the final pipeline."""
        args = context.raw_request.arguments
        arg_defs = context.cmd_config.arguments
        return [
            [
                self.resolve_part(part, args, arg_defs, run_id=context.run_id)
                for part in self.strip_omitted_optionals(step.command, args, arg_defs)
            ]
            for step in context.cmd_config.pipeline
        ]
