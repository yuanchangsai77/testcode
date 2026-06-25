from __future__ import annotations

from testcode.app import create_app
from testcode.context import ExplicitContextLoader
from testcode.model.prompt import ModelPromptBuilder
from testcode.observability.logger import InMemoryLogger
from testcode.orchestration.session import SessionContext
from testcode.types import UserRequest


def test_explicit_context_loader_reads_file_directory_and_glob(tmp_path):
    (tmp_path / "README.md").write_text("readme body\n", encoding="utf-8")
    docs = tmp_path / "docs"
    docs.mkdir()
    (docs / "a.md").write_text("a\n", encoding="utf-8")
    (docs / "b.md").write_text("b\n", encoding="utf-8")
    request = UserRequest(
        prompt="inspect",
        cwd=str(tmp_path),
        metadata={"context_paths": ["README.md", "docs", "docs/*.md"]},
    )
    session = SessionContext(request=request)

    ExplicitContextLoader().load_context(request, session)

    assert [item.kind for item in session.explicit_context] == ["file", "directory", "file", "file"]
    assert session.explicit_context[0].path == "README.md"
    assert session.explicit_context[0].content == "readme body\n"
    assert session.explicit_context[1].path == "docs"
    assert "docs/a.md" in session.explicit_context[1].content
    assert {item.path for item in session.explicit_context[2:]} == {"docs/a.md", "docs/b.md"}


def test_explicit_context_loader_rejects_outside_workspace(tmp_path):
    outside = tmp_path.parent / "outside-context.txt"
    outside.write_text("outside\n", encoding="utf-8")
    request = UserRequest(
        prompt="inspect",
        cwd=str(tmp_path),
        metadata={"context_paths": [str(outside)]},
    )
    session = SessionContext(request=request)

    ExplicitContextLoader().load_context(request, session)

    assert len(session.explicit_context) == 1
    assert session.explicit_context[0].error == "path_outside_workspace"


def test_explicit_context_loader_reports_unmatched_glob(tmp_path):
    request = UserRequest(
        prompt="inspect",
        cwd=str(tmp_path),
        metadata={"context_paths": ["docs/*.md"]},
    )
    session = SessionContext(request=request)

    ExplicitContextLoader().load_context(request, session)

    assert len(session.explicit_context) == 1
    assert session.explicit_context[0].source == "docs/*.md"
    assert session.explicit_context[0].error == "path_not_found"


def test_explicit_context_loader_truncates_and_skips_binary(tmp_path):
    (tmp_path / "long.txt").write_text("abcdef", encoding="utf-8")
    (tmp_path / "binary.bin").write_bytes(b"abc\x00def")
    request = UserRequest(
        prompt="inspect",
        cwd=str(tmp_path),
        metadata={"context_paths": ["long.txt", "binary.bin"]},
    )
    session = SessionContext(request=request)

    ExplicitContextLoader(max_bytes=3).load_context(request, session)

    assert session.explicit_context[0].content == "abc"
    assert session.explicit_context[0].truncated is True
    assert session.explicit_context[1].error == "binary_file"


def test_explicit_context_prompt_section_and_logging(tmp_path):
    (tmp_path / "README.md").write_text("hello\n", encoding="utf-8")
    logger = InMemoryLogger(base_dir=str(tmp_path / "runs"))
    request = UserRequest(
        prompt="inspect",
        cwd=str(tmp_path),
        metadata={"context_paths": ["README.md"]},
    )
    logger.start_run(request)
    session = SessionContext(request=request)

    ExplicitContextLoader(logger=logger).load_context(request, session)
    messages = ModelPromptBuilder().build_messages(session)
    system = str(messages[0]["content"])

    assert "### Explicit User Context:" in system
    assert "[file: README.md]" in system
    assert "hello" in system
    assert system.index("### Explicit User Context:") < system.index("Available tools:")
    assert logger.events[-1].name == "context.explicit"


def test_create_app_registers_explicit_context_loader(tmp_path, monkeypatch):
    monkeypatch.setenv("TESTCODE_MODEL_BASE_URL", "")
    monkeypatch.chdir(tmp_path)

    app = create_app()

    loader_names = [loader.__class__.__name__ for loader in app.engine.context_loaders]
    assert loader_names[:4] == [
        "ProjectRulesLoader",
        "WorkspaceSummaryLoader",
        "ExplicitContextLoader",
        "SkillContextLoader",
    ]
