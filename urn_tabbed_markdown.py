from comfy_api.latest import io


class URNTabbedMarkdown(io.ComfyNode):
    """Presentation-only tabbed Markdown note for documenting ComfyUI workflows."""

    @classmethod
    def define_schema(cls) -> io.Schema:
        return io.Schema(
            node_id="URNTabbedMarkdown",
            display_name="URN Tabbed Markdown",
            category="URN Audio Tools",
            description=(
                "Display-only tabbed Markdown note. Add/remove tabs, double-click a tab to rename it, "
                "and double-click the note area to edit the active tab. The note has no inputs or outputs "
                "and does not process workflow data."
            ),
            is_output_node=False,
            inputs=[],
            outputs=[],
        )

    @classmethod
    def execute(cls) -> io.NodeOutput:
        # This node is presentation-only and has no execution connections. Keeping
        # a no-op execute method makes the backend definition complete if a client
        # ever includes it in a prompt unexpectedly.
        return io.NodeOutput()
