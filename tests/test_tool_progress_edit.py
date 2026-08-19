from __future__ import annotations

import json

from app.stream.tool_progress import format_tool_line


def test_edit_batch_progress_summary_uses_path_and_segment_count():
    arguments = json.dumps({
        "path": "app/example.py",
        "edits": [
            {"old_string": "a", "new_string": "b"},
            {"old_string": "c", "new_string": "d", "replace_all": True},
        ],
    })

    assert format_tool_line("EditBatch", arguments) == "✏️ EditBatch: app/example.py · 2 段"


def test_single_edit_progress_summary_stays_path_based():
    arguments = json.dumps({"path": "app/example.py", "old_string": "a", "new_string": "b"})

    assert format_tool_line("Edit", arguments) == "✏️ Edit: app/example.py"
