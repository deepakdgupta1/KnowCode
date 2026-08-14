"""Step 22 release gate: security invariants through production entry points.

Steps 19-21 established the three security boundaries in isolation. This gate
drives them through the production entry points over the adversarial repository,
which carries hostile code comments and a secret-shaped token in real source:

* **Prompt hierarchy (Step 19).** The exact path ``agent.answer`` takes —
  ``retrieve_context_for_query`` → ``context_text`` → ``build_prompt_request`` —
  keeps KnowCode's instructions in the system channel and every byte of
  retrieved repository content in the untrusted-data channel, as one escaped
  physical line. A comment written as a system prompt cannot become one.
* **Telemetry non-disclosure (Step 20).** After a real query over a repository
  whose *source* contains a credential-shaped token and injection comments,
  none of that text — nor the question — is on disk.

Rate limiting through the real server stack with the hostile input (spoofed
forwarding headers) is proven in ``tests/integration/test_rate_limit_server.py``
and is not duplicated here.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from knowcode import telemetry
from knowcode.config import AppConfig
from knowcode.data_models import TaskType
from knowcode.llm.prompt_contract import (
    build_prompt_request,
    format_provider_error,
    google_request_kwargs,
    openai_messages,
)
from knowcode.service import KnowCodeService

from tests.helpers.adversarial_repo import (
    FIXTURE_SECRET,
    HOSTILE_MARKERS,
    build_adversarial_repo,
)

QUERY = "how does placing an order work in OrderService"


@pytest.fixture
def service(tmp_path: Path):  # type: ignore[no-untyped-def]
    repo = build_adversarial_repo(tmp_path)
    built = KnowCodeService(store_path=repo.output, app_config=AppConfig.default())
    stats = built.analyze(directory=repo.source, output=repo.output)
    assert stats["published"] is True, stats
    try:
        yield built
    finally:
        built.close()
        telemetry.shutdown_telemetry(timeout=5.0)


# ----------------------------------------------------------------------
# Prompt hierarchy through the production retrieval → request path
# ----------------------------------------------------------------------


def test_retrieved_repository_content_stays_in_the_data_channel(
    service: KnowCodeService,
) -> None:
    """Real retrieved code lands in the untrusted payload, never in instructions."""
    retrieval = service.retrieve_context_for_query(QUERY)
    context = retrieval.get("context_text", "")
    assert "OrderService" in context, "precondition: retrieval surfaced the code"

    task_type = TaskType(retrieval.get("task_type") or TaskType.GENERAL.value)
    request = build_prompt_request(task_type=task_type, question=QUERY, context=context)

    # Instructions are KnowCode's alone: no retrieved code, no question.
    assert context not in request.system_instruction
    assert "OrderService" not in request.system_instruction
    assert QUERY not in request.system_instruction

    # Every byte of retrieved content and the question travel as untrusted data.
    assert "OrderService" in request.user_payload
    assert QUERY in request.user_payload

    # The whole user turn is one physical line, so retrieved text cannot inject
    # a newline-delimited role marker or conversation turn.
    assert "\n" not in request.user_payload


def test_both_provider_serializers_keep_the_channels_separate(
    service: KnowCodeService,
) -> None:
    retrieval = service.retrieve_context_for_query(QUERY)
    context = retrieval.get("context_text", "")
    request = build_prompt_request(
        task_type=TaskType.GENERAL, question=QUERY, context=context
    )

    google = google_request_kwargs(request)
    assert google["config"]["system_instruction"] == request.system_instruction
    assert google["contents"] == [request.user_payload]
    assert "OrderService" not in str(google["config"])

    messages = openai_messages(request)
    assert messages[0]["role"] == "system"
    assert messages[0]["content"] == request.system_instruction
    assert messages[1]["role"] == "user"
    assert messages[1]["content"] == request.user_payload
    assert "OrderService" not in messages[0]["content"]


def test_hostile_repository_comments_cannot_escape_the_data_channel() -> None:
    """The repo's exact injection payloads stay data even in the worst case.

    Retrieval need not surface a comment for this to matter: whenever it does,
    the marker must be a JSON string value inside ``repository_context``, never a
    line the model could read as an instruction. Feeding every marker at once is
    the worst case.
    """
    import json

    hostile_context = "\n".join(HOSTILE_MARKERS)
    request = build_prompt_request(
        task_type=TaskType.GENERAL,
        question="summarize this file",
        context=hostile_context,
    )

    for marker in HOSTILE_MARKERS:
        assert marker not in request.system_instruction, (
            f"a hostile marker reached the instruction channel: {marker!r}"
        )
        assert marker in request.user_payload, (
            f"a hostile marker was dropped from the data channel: {marker!r}"
        )

    # The payload is one physical line and a well-formed JSON object whose
    # repository_context field carries the markers as a value, not as structure.
    assert "\n" not in request.user_payload
    envelope = json.loads(request.user_payload)
    assert envelope["repository_context"]["text"] == hostile_context
    assert envelope["knowcode_untrusted_input_version"] == 1


def test_a_provider_error_quoting_the_repo_is_bounded(service: KnowCodeService) -> None:
    """A provider that echoes the request in its error cannot flood the console.

    Step 19 bounds rather than eliminates this: the rendered error is the
    exception type plus a message collapsed to one line and capped. The residual
    (a provider replaying up to the cap) is the user's own repository content on
    the user's own terminal, and it is bounded here.
    """
    replayed = HOSTILE_MARKERS[0] * 20  # far longer than the cap
    rendered = format_provider_error(RuntimeError(replayed))

    assert "\n" not in rendered
    assert len(rendered) <= len("RuntimeError: ") + 200 + len("… (truncated)")
    assert rendered.startswith("RuntimeError:")


# ----------------------------------------------------------------------
# Telemetry non-disclosure of repository-sourced secrets
# ----------------------------------------------------------------------


def _telemetry_payload(root: Path) -> str:
    files = sorted(root.rglob("*.jsonl*"))
    return "".join(
        path.read_text(encoding="utf-8", errors="replace") for path in files
    )


def test_repository_sourced_secret_is_never_persisted_by_telemetry(
    service: KnowCodeService, tmp_path: Path
) -> None:
    """A credential-shaped token *in the indexed code* stays off disk.

    The existing suite proves a secret in the *question* is not persisted. This
    proves the complementary case: the token lives in ``orders.py`` and is
    retrievable, yet the telemetry schema records only aggregate metadata, so
    neither it nor the injection comments nor the question reach disk.
    """
    service.retrieve_context_for_query(QUERY)
    telemetry.shutdown_telemetry(timeout=5.0)

    payload = _telemetry_payload(tmp_path)
    assert payload, "the run wrote no telemetry at all"
    assert FIXTURE_SECRET not in payload
    for marker in HOSTILE_MARKERS:
        assert marker not in payload
    assert QUERY not in payload
    assert "OrderService" not in payload


def test_one_adversarial_query_is_counted_exactly_once(
    service: KnowCodeService, tmp_path: Path
) -> None:
    before = telemetry.get_telemetry_summary(tmp_path)["total_queries"]

    service.retrieve_context_for_query(QUERY)
    telemetry.shutdown_telemetry(timeout=5.0)

    after = telemetry.get_telemetry_summary(tmp_path)["total_queries"]
    assert after == before + 1


def test_deleting_telemetry_removes_everything_the_query_wrote(
    service: KnowCodeService, tmp_path: Path
) -> None:
    service.retrieve_context_for_query(QUERY)
    telemetry.shutdown_telemetry(timeout=5.0)

    telemetry.delete_telemetry(tmp_path)

    assert sorted(tmp_path.rglob("*.jsonl*")) == []
    assert telemetry.get_telemetry_summary(tmp_path)["total_queries"] == 0
