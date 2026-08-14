"""Durable, provider-neutral NPC conversation runtimes.

The MCP owns semantic continuity and actor isolation.  Model processes and
provider KV caches remain host concerns; a host can rebuild either from the
actor-scoped bootstrap plus the durable event journal kept here.
"""

from __future__ import annotations

import hashlib
import hmac
import json
import os
import secrets
import threading
import time
import zlib
from base64 import b64decode, b64encode
from copy import deepcopy
from pathlib import Path
from typing import Any
from uuid import uuid4

NPC_CONVERSATION_SCHEMA_VERSION = 3
NPC_CONVERSATION_PROPOSAL_SCHEMA_VERSION = 4
NPC_CONVERSATION_CONTRACT = "npc-conversation.v3"

NPC_RESOLUTION_KINDS = frozenset(
    {
        "skill_check",
        "opposed_check",
        "sanity_check",
        "luck_check",
        "chase_check",
        "attack",
        "keeper_adjudication",
    }
)
ACTIVE_CONVERSATION_STATUSES = frozenset({"open", "stale"})
MAX_CONVERSATION_EVENTS = 200
MAX_ACTIVE_JOURNAL_BYTES = 4 * 1024 * 1024
TERMINAL_RECEIPT_RETENTION_NS = 30 * 24 * 60 * 60 * 1_000_000_000
TERMINAL_RESULT_CODEC = "zlib-json-v1"


def _object(value: Any, field: str) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise ValueError(f"{field} must be an object")
    return dict(value)


def _strict(value: dict[str, Any], field: str, allowed: set[str]) -> None:
    unknown = set(value) - allowed
    if unknown:
        raise ValueError(f"{field} has unknown fields: {sorted(unknown)}")


def _text(
    value: Any,
    field: str,
    *,
    required: bool = False,
    maximum: int = 4_000,
) -> str:
    result = str(value or "").strip()
    if required and not result:
        raise ValueError(f"{field} is required")
    if len(result) > maximum:
        raise ValueError(f"{field} exceeds {maximum} characters")
    return result


def _string_list(value: Any, field: str, *, maximum: int = 200) -> list[str]:
    if value is None:
        return []
    if not isinstance(value, list):
        raise ValueError(f"{field} must be a list")
    result = [_text(item, f"{field}[]", required=True, maximum=maximum) for item in value]
    if len(result) != len(set(result)):
        raise ValueError(f"{field} must not contain duplicates")
    return result


def normalize_conversation_proposal(value: Any) -> dict[str, Any]:
    """Normalize the minimal authoritative v4 NPC proposal contract."""

    data = _object(value, "npc_conversation.proposal")
    allowed = {
        "schema_version",
        "conversation_id",
        "activation_id",
        "actor_runtime_id",
        "response_bid",
        "private_intent",
        "utterance_segments",
        "proposed_action",
        "resolution_requests",
        "working_deltas",
        "visible_cues",
        "decision_summary",
    }
    _strict(data, "npc_conversation.proposal", allowed)
    if data.get("schema_version") != NPC_CONVERSATION_PROPOSAL_SCHEMA_VERSION:
        raise ValueError("npc_conversation.proposal.schema_version must be 4")

    response_bid = _object(data.get("response_bid") or {}, "response_bid")
    _strict(response_bid, "response_bid", {"should_respond", "urgency", "reason"})
    if not isinstance(response_bid.get("should_respond"), bool):
        raise ValueError("response_bid.should_respond must be boolean")
    urgency = response_bid.get("urgency", 0)
    if type(urgency) is not int or not 0 <= urgency <= 100:
        raise ValueError("response_bid.urgency must be an integer from 0 to 100")

    raw_segments = data.get("utterance_segments") or []
    if not isinstance(raw_segments, list) or len(raw_segments) > 12:
        raise ValueError("utterance_segments must be a list with at most 12 items")
    segments: list[dict[str, Any]] = []
    for index, raw in enumerate(raw_segments):
        item = _object(raw, f"utterance_segments[{index}]")
        _strict(
            item,
            f"utterance_segments[{index}]",
            {
                "text",
                "speech_act",
                "truth_posture",
                "basis_refs",
                "targets",
                "language",
                "delivery",
            },
        )
        basis_refs = _string_list(
            item.get("basis_refs"),
            f"utterance_segments[{index}].basis_refs",
            maximum=300,
        )
        segments.append(
            {
                "text": _text(
                    item.get("text"),
                    f"utterance_segments[{index}].text",
                    required=True,
                    maximum=2_000,
                ),
                "speech_act": _text(
                    item.get("speech_act"),
                    f"utterance_segments[{index}].speech_act",
                    maximum=100,
                ),
                "truth_posture": _text(
                    item.get("truth_posture"),
                    f"utterance_segments[{index}].truth_posture",
                    maximum=100,
                ),
                "basis_refs": basis_refs,
                "targets": _string_list(
                    item.get("targets"), f"utterance_segments[{index}].targets"
                ),
                "language": _text(
                    item.get("language"), f"utterance_segments[{index}].language", maximum=100
                ),
                "delivery": _text(
                    item.get("delivery"), f"utterance_segments[{index}].delivery", maximum=500
                ),
            }
        )

    action = _object(data.get("proposed_action") or {}, "proposed_action")
    _strict(
        action,
        "proposed_action",
        {"summary", "target_refs", "settlement", "mechanic_hint"},
    )
    action_summary = _text(action.get("summary"), "proposed_action.summary", maximum=1_000)
    settlement = (
        _text(action.get("settlement"), "proposed_action.settlement", maximum=20) or "narrative"
    )
    if settlement not in {"narrative", "mechanical"}:
        raise ValueError("proposed_action.settlement must be narrative or mechanical")
    target_refs = _string_list(
        action.get("target_refs"), "proposed_action.target_refs", maximum=300
    )

    raw_requests = data.get("resolution_requests") or []
    if not isinstance(raw_requests, list) or len(raw_requests) > 8:
        raise ValueError("resolution_requests must be a list with at most 8 items")
    requests: list[dict[str, Any]] = []
    for index, raw in enumerate(raw_requests):
        item = _object(raw, f"resolution_requests[{index}]")
        _strict(item, f"resolution_requests[{index}]", {"kind", "reason", "actor_ids"})
        kind = _text(item.get("kind"), f"resolution_requests[{index}].kind", required=True)
        if kind not in NPC_RESOLUTION_KINDS:
            raise ValueError(f"unsupported NPC resolution kind: {kind}")
        requests.append(
            {
                "kind": kind,
                "reason": _text(
                    item.get("reason"),
                    f"resolution_requests[{index}].reason",
                    required=True,
                    maximum=1_000,
                ),
                "actor_ids": _string_list(
                    item.get("actor_ids"), f"resolution_requests[{index}].actor_ids"
                ),
            }
        )
    if settlement == "mechanical" and action_summary and not requests:
        raise ValueError("a mechanical NPC action requires an explicit resolution request")

    deltas = _object(data.get("working_deltas") or {}, "working_deltas")
    _strict(deltas, "working_deltas", {"facts", "actor_knowledge", "commitments"})
    normalized_deltas: dict[str, list[dict[str, Any]]] = {}
    for field in ("facts", "actor_knowledge", "commitments"):
        raw_items = deltas.get(field) or []
        if not isinstance(raw_items, list) or not all(isinstance(item, dict) for item in raw_items):
            raise ValueError(f"working_deltas.{field} must be a list of objects")
        if len(raw_items) > 20:
            raise ValueError(f"working_deltas.{field} exceeds 20 items")
        normalized_deltas[field] = [deepcopy(dict(item)) for item in raw_items]

    should_respond = response_bid["should_respond"]
    if should_respond and not segments and not action_summary and not requests:
        raise ValueError("responding NPC must speak, act, or request resolution")
    if not should_respond and (segments or action_summary or requests):
        raise ValueError("non-responding NPC must not speak, act, or request resolution")

    return {
        "schema_version": NPC_CONVERSATION_PROPOSAL_SCHEMA_VERSION,
        "conversation_id": _text(
            data.get("conversation_id"), "conversation_id", required=True, maximum=100
        ),
        "activation_id": _text(
            data.get("activation_id"), "activation_id", required=True, maximum=100
        ),
        "actor_runtime_id": _text(
            data.get("actor_runtime_id"), "actor_runtime_id", required=True, maximum=220
        ),
        "response_bid": {
            "should_respond": should_respond,
            "urgency": urgency,
            "reason": _text(response_bid.get("reason"), "response_bid.reason", maximum=500),
        },
        "private_intent": _text(data.get("private_intent"), "private_intent", maximum=1_000),
        "utterance_segments": segments,
        "proposed_action": {
            "summary": action_summary,
            "target_refs": target_refs,
            "settlement": settlement,
            "mechanic_hint": _text(
                action.get("mechanic_hint"), "proposed_action.mechanic_hint", maximum=500
            ),
        },
        "resolution_requests": requests,
        "working_deltas": normalized_deltas,
        "visible_cues": _string_list(data.get("visible_cues"), "visible_cues", maximum=500),
        "decision_summary": _text(data.get("decision_summary"), "decision_summary", maximum=500),
    }


def validate_conversation_proposal(
    proposal: dict[str, Any],
    *,
    conversation_id: str,
    activation_id: str,
    actor_runtime_id: str,
    actor_id: str,
    allowed_basis_refs: set[str],
    allowed_actor_ids: set[str],
) -> None:
    if proposal["conversation_id"] != conversation_id:
        raise ValueError("NPC proposal belongs to another conversation")
    if proposal["activation_id"] != activation_id:
        raise ValueError("NPC proposal belongs to another activation")
    if proposal["actor_runtime_id"] != actor_runtime_id:
        raise ValueError("NPC proposal belongs to another actor runtime")
    cited_basis = {
        ref for segment in proposal["utterance_segments"] for ref in segment["basis_refs"]
    }
    if unknown := sorted(cited_basis - allowed_basis_refs):
        raise ValueError(f"NPC proposal cites basis refs outside its actor capsule: {unknown}")
    cited_actor_ids = {
        target for segment in proposal["utterance_segments"] for target in segment["targets"]
    }
    cited_actor_ids.update(
        item for request in proposal["resolution_requests"] for item in request["actor_ids"]
    )
    if unknown := sorted(cited_actor_ids - allowed_actor_ids):
        raise ValueError(f"NPC proposal cites actors outside its conversation: {unknown}")
    allowed_target_refs = {f"actor:{item}" for item in allowed_actor_ids}
    if unknown := sorted(
        set(proposal["proposed_action"].get("target_refs") or []) - allowed_target_refs
    ):
        raise ValueError(f"NPC proposal action targets outside its conversation: {unknown}")
    for index, item in enumerate(proposal["working_deltas"]["facts"]):
        if str(item.get("subject_ref") or "") != f"actor:{actor_id}":
            raise ValueError(f"working_deltas.facts[{index}] must belong to the speaking actor")
        if str(item.get("kind") or "") != "actor_state":
            raise ValueError(f"working_deltas.facts[{index}] must use kind='actor_state'")
        if str(item.get("predicate") or "") not in {
            "relationship_to",
            "goal",
            "commitment",
        }:
            raise ValueError(
                f"working_deltas.facts[{index}] may update only relationships, goals, "
                "or commitments"
            )
    for index, item in enumerate(proposal["working_deltas"]["actor_knowledge"]):
        if str(item.get("actor_id") or "") != actor_id:
            raise ValueError(
                f"working_deltas.actor_knowledge[{index}] must belong to the speaking actor"
            )
    for index, item in enumerate(proposal["working_deltas"]["commitments"]):
        if str(item.get("actor_id") or "") != actor_id:
            raise ValueError(
                f"working_deltas.commitments[{index}] must belong to the speaking actor"
            )
        _text(
            item.get("commitment_key"),
            f"working_deltas.commitments[{index}].commitment_key",
            required=True,
            maximum=200,
        )
        _text(
            item.get("content"),
            f"working_deltas.commitments[{index}].content",
            required=True,
            maximum=2_000,
        )


def derive_publication(proposal: dict[str, Any], *, publication_id: str) -> dict[str, Any]:
    """Return the only model output that a Director may show to players."""

    segments = [
        {
            "text": item["text"],
            "speech_act": item["speech_act"],
            "targets": list(item["targets"]),
            "language": item["language"],
            "delivery": item["delivery"],
        }
        for item in proposal["utterance_segments"]
    ]
    action = proposal["proposed_action"]
    return {
        "schema_version": 1,
        "publication_id": publication_id,
        "conversation_id": proposal["conversation_id"],
        "activation_id": proposal["activation_id"],
        "actor_runtime_id": proposal["actor_runtime_id"],
        "utterance_segments": segments,
        "speech": " ".join(item["text"] for item in segments),
        "visible_cues": list(proposal["visible_cues"]),
        "visible_action": str(action.get("summary") or ""),
        "action_settlement": str(action.get("settlement") or "narrative"),
        "action_target_refs": list(action.get("target_refs") or []),
    }


def normalize_audience_facts(
    value: Any,
    *,
    participant_ids: set[str],
    response_actor_ids: set[str],
) -> dict[str, Any]:
    """Validate an Agent ruling without deriving perception or comprehension."""

    data = _object(value, "audience_facts")
    _strict(
        data,
        "audience_facts",
        {
            "decision_id",
            "resolver",
            "perceived_actor_ids",
            "understood_actor_ids",
            "response_actor_ids",
            "partial_renditions",
            "basis_refs",
            "reason",
        },
    )
    if _text(data.get("resolver"), "audience_facts.resolver", required=True) != "agent":
        raise ValueError("audience_facts.resolver must be agent")
    perceived = set(
        _string_list(data.get("perceived_actor_ids"), "audience_facts.perceived_actor_ids")
    )
    understood = set(
        _string_list(data.get("understood_actor_ids"), "audience_facts.understood_actor_ids")
    )
    response = set(
        _string_list(data.get("response_actor_ids"), "audience_facts.response_actor_ids")
    )
    if unknown := sorted((perceived | understood | response) - participant_ids):
        raise ValueError(f"audience_facts cites actors outside the conversation: {unknown}")
    if not understood <= perceived:
        raise ValueError("understood_actor_ids must be a subset of perceived_actor_ids")
    if not response <= perceived:
        raise ValueError("response_actor_ids must be a subset of perceived_actor_ids")
    if unknown := sorted(response - response_actor_ids):
        raise ValueError(f"response_actor_ids contains actors without NPC runtimes: {unknown}")
    partial_raw = _object(data.get("partial_renditions") or {}, "partial_renditions")
    partial: dict[str, str] = {}
    for actor_id, rendition in partial_raw.items():
        if actor_id not in perceived - understood:
            raise ValueError(
                "partial_renditions may target only perceived actors who did not fully understand"
            )
        partial[actor_id] = _text(
            rendition,
            f"partial_renditions.{actor_id}",
            required=True,
            maximum=2_000,
        )
    return {
        "decision_id": _text(
            data.get("decision_id"), "audience_facts.decision_id", required=True, maximum=100
        ),
        "resolver": "agent",
        "perceived_actor_ids": sorted(perceived),
        "understood_actor_ids": sorted(understood),
        "response_actor_ids": sorted(response),
        "partial_renditions": partial,
        "basis_refs": _string_list(
            data.get("basis_refs"), "audience_facts.basis_refs", maximum=300
        ),
        "reason": _text(data.get("reason"), "audience_facts.reason", required=True, maximum=1_000),
    }


class ConversationStore:
    """Atomic JSON journal for active, not-yet-authoritative conversations."""

    def __init__(self, root: Path, *, lease_ttl_s: int = 120) -> None:
        self.root = root.resolve()
        self.root.mkdir(parents=True, exist_ok=True)
        self.lease_ttl_s = max(10, int(lease_ttl_s))
        self._lock = threading.RLock()
        self._secret = self._load_secret()
        self.cleanup_terminal_receipts()

    def _load_secret(self) -> bytes:
        path = self.root / ".capability-key"
        if path.exists():
            value = path.read_bytes()
            if len(value) >= 32:
                return value
            raise RuntimeError("NPC conversation capability key is invalid")
        value = secrets.token_bytes(32)
        temporary = path.with_name(f".{path.name}.{uuid4().hex}.tmp")
        temporary.write_bytes(value)
        os.replace(temporary, path)
        return value

    def _path(self, conversation_id: str) -> Path:
        if not conversation_id or any(ch not in "0123456789abcdef-" for ch in conversation_id):
            raise ValueError("invalid conversation_id")
        return self.root / f"{conversation_id}.json"

    def _write(self, session: dict[str, Any]) -> None:
        path = self._path(str(session["conversation_id"]))
        temporary = path.with_name(f".{path.name}.{uuid4().hex}.tmp")
        encoded = json.dumps(session, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
        if (
            session.get("status") in ACTIVE_CONVERSATION_STATUSES
            and len(encoded.encode("utf-8")) > MAX_ACTIVE_JOURNAL_BYTES
        ):
            raise ValueError(
                "conversation journal exceeds 4194304 bytes; close or abort the conversation"
            )
        with temporary.open("w", encoding="utf-8", newline="\n") as handle:
            handle.write(encoded)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)

    @staticmethod
    def _is_current_session(value: Any) -> bool:
        return (
            isinstance(value, dict)
            and value.get("schema_version") == NPC_CONVERSATION_SCHEMA_VERSION
            and value.get("contract") == NPC_CONVERSATION_CONTRACT
        )

    def _current_sessions(self):
        for path in self.root.glob("*.json"):
            try:
                value = json.loads(path.read_text(encoding="utf-8"))
            except (OSError, json.JSONDecodeError):
                continue
            if self._is_current_session(value):
                yield value

    def cleanup_terminal_receipts(self, *, now_ns: int | None = None) -> int:
        cutoff = int(now_ns if now_ns is not None else time.time_ns())
        removed = 0
        with self._lock:
            for path in self.root.glob("*.json"):
                try:
                    value = json.loads(path.read_text(encoding="utf-8"))
                except (OSError, json.JSONDecodeError):
                    continue
                if (
                    isinstance(value, dict)
                    and str(value.get("contract") or "").startswith("npc-conversation.v")
                    and not self._is_current_session(value)
                ):
                    path.unlink()
                    removed += 1
                    continue
                if (
                    self._is_current_session(value)
                    and value.get("status") in {"closed", "aborted"}
                    and cutoff - int(value.get("updated_at_ns") or 0)
                    > TERMINAL_RECEIPT_RETENTION_NS
                ):
                    path.unlink()
                    removed += 1
        return removed

    @staticmethod
    def _encode_terminal_result(result: dict[str, Any]) -> str:
        raw = json.dumps(
            result,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")
        return b64encode(zlib.compress(raw)).decode("ascii")

    @staticmethod
    def _decode_idempotency_result(entry: dict[str, Any]) -> dict[str, Any]:
        if entry.get("result_codec") == TERMINAL_RESULT_CODEC:
            raw = zlib.decompress(b64decode(str(entry["compressed_result"])))
            value = json.loads(raw.decode("utf-8"))
            if not isinstance(value, dict):
                raise RuntimeError("terminal conversation result is invalid")
            return value
        return deepcopy(entry["result"])

    @staticmethod
    def _compact_terminal_session(
        session: dict[str, Any],
        *,
        terminal_key: str,
        fingerprint: str,
        result: dict[str, Any],
    ) -> dict[str, Any]:
        return {
            key: deepcopy(session[key])
            for key in (
                "schema_version",
                "contract",
                "conversation_id",
                "campaign_id",
                "branch_id",
                "principal_id",
                "scope_id",
                "scene_id",
                "status",
                "conversation_revision",
                "created_at_ns",
                "updated_at_ns",
                "authority",
                "participants",
                "participant_ids",
                "open_idempotency_key",
                "open_fingerprint",
            )
        } | {
            "actor_runtimes": {},
            "events": [],
            "activations": {},
            "publications": [],
            "pending_resolutions": [],
            "memory_candidates": [],
            "audience_decision_ids": [],
            "terminal_receipt": {
                "event_id": str(dict(result.get("event") or {}).get("id") or ""),
            },
            "idempotency": {
                terminal_key: {
                    "fingerprint": fingerprint,
                    "result_codec": TERMINAL_RESULT_CODEC,
                    "compressed_result": ConversationStore._encode_terminal_result(result),
                }
            },
        }

    def get(self, conversation_id: str) -> dict[str, Any]:
        with self._lock:
            path = self._path(conversation_id)
            if not path.is_file():
                raise LookupError(conversation_id)
            value = json.loads(path.read_text(encoding="utf-8"))
            if not self._is_current_session(value):
                raise LookupError(conversation_id)
            return value

    def save(self, session: dict[str, Any]) -> None:
        with self._lock:
            self._write(session)

    def active_ids(self, *, campaign_id: str, branch_id: str) -> list[str]:
        """Return active conversation ids without exposing private journal state."""

        with self._lock:
            result: list[str] = []
            for session in self._current_sessions():
                if (
                    session.get("campaign_id") == campaign_id
                    and session.get("branch_id") == branch_id
                    and session.get("status") in ACTIVE_CONVERSATION_STATUSES
                ):
                    result.append(str(session["conversation_id"]))
            return sorted(result)

    def active_public_statuses(
        self,
        *,
        campaign_id: str,
        branch_id: str,
        principal_id: str,
    ) -> list[dict[str, Any]]:
        """Return public recovery handles for conversations owned by one principal."""

        with self._lock:
            result = [
                self.public_status(session)
                for session in self._current_sessions()
                if session.get("campaign_id") == campaign_id
                and session.get("branch_id") == branch_id
                and session.get("principal_id") == principal_id
                and session.get("status") in ACTIVE_CONVERSATION_STATUSES
            ]
            return sorted(result, key=lambda item: str(item["conversation_id"]))

    @staticmethod
    def _fingerprint(value: Any) -> str:
        encoded = json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
        return hashlib.sha256(encoded.encode("utf-8")).hexdigest()

    def begin_mutation(
        self,
        conversation_id: str,
        *,
        expected_revision: int,
        idempotency_key: str,
        operation: str,
        payload: Any,
    ) -> tuple[dict[str, Any], dict[str, Any] | None]:
        """Load one journal revision and replay only an identical prior write."""

        if not idempotency_key:
            raise ValueError("idempotency_key is required")
        with self._lock:
            session = self.get(conversation_id)
            fingerprint = self._fingerprint({"operation": operation, "payload": payload})
            prior = dict(session.get("idempotency") or {}).get(idempotency_key)
            if prior is not None:
                if prior.get("fingerprint") != fingerprint:
                    raise ValueError("idempotency_key was already used with another payload")
                return session, self._decode_idempotency_result(prior)
            if int(session.get("conversation_revision") or 0) != int(expected_revision):
                raise ValueError(
                    "CONVERSATION_REVISION_CONFLICT: expected "
                    f"{expected_revision}, current {session.get('conversation_revision')}"
                )
            session["_pending_mutation"] = {
                "key": idempotency_key,
                "fingerprint": fingerprint,
            }
            return session, None

    def finish_mutation(self, session: dict[str, Any], result: dict[str, Any]) -> dict[str, Any]:
        """Compare-and-swap one in-memory mutation into the durable journal."""

        pending = dict(session.pop("_pending_mutation", {}))
        if not pending:
            raise RuntimeError("conversation mutation was not prepared")
        with self._lock:
            current = self.get(str(session["conversation_id"]))
            if int(current.get("conversation_revision") or 0) != int(
                session.get("conversation_revision") or 0
            ):
                raise ValueError("CONVERSATION_REVISION_CONFLICT: journal changed during write")
            session["conversation_revision"] = int(session["conversation_revision"]) + 1
            session["updated_at_ns"] = time.time_ns()
            result = {**deepcopy(result), "conversation_revision": session["conversation_revision"]}
            session.setdefault("idempotency", {})[str(pending["key"])] = {
                "fingerprint": str(pending["fingerprint"]),
                "result": deepcopy(result),
            }
            if session.get("status") in {"closed", "aborted"}:
                session = self._compact_terminal_session(
                    session,
                    terminal_key=str(pending["key"]),
                    fingerprint=str(pending["fingerprint"]),
                    result=result,
                )
            self._write(session)
            return result

    @staticmethod
    def _candidate_status(session: dict[str, Any], candidate: dict[str, Any]) -> str:
        if candidate.get("status") == "invalidated":
            return "invalidated"
        publication_id = str(candidate.get("source_publication_id") or "")
        if publication_id:
            publication = next(
                (
                    item
                    for item in session.get("publications") or []
                    if str(item.get("publication_id") or "") == publication_id
                ),
                None,
            )
            if publication is None or publication.get("status") != "published":
                return "pending_publication"
        activation_id = str(candidate.get("source_activation_id") or "")
        if activation_id and any(
            str(item.get("activation_id") or "") == activation_id
            and item.get("status") == "pending"
            for item in session.get("pending_resolutions") or []
        ):
            return "pending_resolution"
        return "available"

    @classmethod
    def memory_candidates(
        cls,
        session: dict[str, Any],
        *,
        actor_id: str | None = None,
        include_invalidated: bool = False,
    ) -> list[dict[str, Any]]:
        result = []
        for raw in session.get("memory_candidates") or []:
            if actor_id is not None and str(raw.get("actor_id") or "") != actor_id:
                continue
            status = cls._candidate_status(session, raw)
            if status == "invalidated" and not include_invalidated:
                continue
            result.append(
                {
                    **deepcopy(raw),
                    "status": status,
                }
            )
        return result

    @staticmethod
    def _append_memory_candidate(
        session: dict[str, Any],
        *,
        actor_id: str,
        kind: str,
        value: dict[str, Any],
        source_activation_id: str = "",
        source_publication_id: str = "",
        source_event_id: str = "",
    ) -> dict[str, Any]:
        candidate = {
            "candidate_id": str(uuid4()),
            "actor_id": actor_id,
            "kind": kind,
            "value": deepcopy(value),
            "source_activation_id": source_activation_id,
            "source_publication_id": source_publication_id,
            "source_event_id": source_event_id,
            "status": "proposed",
        }
        session.setdefault("memory_candidates", []).append(candidate)
        return candidate

    @classmethod
    def _append_heard_statement_candidates(
        cls,
        session: dict[str, Any],
        *,
        event: dict[str, Any],
        segments: list[dict[str, Any]],
        segment_audience_facts: list[dict[str, Any]],
        source_publication_id: str = "",
    ) -> None:
        speaker_id = str(event.get("speaker_actor_id") or "")
        for index, (segment, facts) in enumerate(
            zip(segments, segment_audience_facts, strict=True)
        ):
            for actor_id in facts["understood_actor_ids"]:
                if actor_id == speaker_id:
                    continue
                cls._append_memory_candidate(
                    session,
                    actor_id=actor_id,
                    kind="actor_knowledge",
                    value={
                        "action": "add",
                        "actor_id": actor_id,
                        "knowledge_key": (
                            f"conversation:{session['conversation_id']}:event:"
                            f"{event['event_id']}:segment:{index}"
                        ),
                        "proposition": f"{speaker_id} said: {segment['text']}",
                        "subject_ref": f"actor:{speaker_id}",
                        "epistemic_status": "known",
                        "confidence": 3,
                        "cause": event["event_id"],
                        "disclosure_scope": "owner",
                        "metadata": {
                            "claim_kind": "heard_statement",
                            "audience_decision_id": facts["decision_id"],
                            "statement_truth_not_implied": True,
                        },
                    },
                    source_publication_id=source_publication_id,
                    source_event_id=str(event["event_id"]),
                )

    def open(
        self,
        *,
        campaign_id: str,
        branch_id: str,
        principal_id: str,
        scope_id: str,
        scene_id: str,
        authority: dict[str, Any],
        participants: list[dict[str, Any]],
        actor_contexts: dict[str, dict[str, Any]],
        idempotency_key: str,
    ) -> dict[str, Any]:
        with self._lock:
            if not idempotency_key:
                raise ValueError("idempotency_key is required")
            participant_ids = [str(item["actor_id"]) for item in participants]
            open_fingerprint = self._fingerprint(
                {
                    "campaign_id": campaign_id,
                    "branch_id": branch_id,
                    "principal_id": principal_id,
                    "scope_id": scope_id,
                    "participants": participant_ids,
                }
            )
            for existing in self._current_sessions():
                if (
                    existing.get("campaign_id") == campaign_id
                    and existing.get("principal_id") == principal_id
                    and existing.get("open_idempotency_key") == idempotency_key
                ):
                    if existing.get("open_fingerprint") != open_fingerprint:
                        raise ValueError(
                            "idempotency_key was already used to open another conversation"
                        )
                    return self.public_status(existing)
                if (
                    existing.get("campaign_id") == campaign_id
                    and existing.get("branch_id") == branch_id
                    and existing.get("status") in ACTIVE_CONVERSATION_STATUSES
                    and set(existing.get("participant_ids") or []) & set(participant_ids)
                ):
                    raise ValueError(
                        "an actor is already participating in another active conversation"
                    )
            conversation_id = str(uuid4())
            now_ns = time.time_ns()
            runtimes = {}
            for actor_id, context in actor_contexts.items():
                runtimes[actor_id] = {
                    "actor_runtime_id": f"{conversation_id}:{actor_id}",
                    "actor_id": actor_id,
                    "status": "idle",
                    "inbox_cursor": 0,
                    "working_state_revision": 0,
                    "working_state": {
                        "facts": [],
                        "actor_knowledge": [],
                        "commitments": [],
                    },
                    "context": deepcopy(context),
                    }
            session = {
                "schema_version": NPC_CONVERSATION_SCHEMA_VERSION,
                "contract": NPC_CONVERSATION_CONTRACT,
                "conversation_id": conversation_id,
                "campaign_id": campaign_id,
                "branch_id": branch_id,
                "principal_id": principal_id,
                "scope_id": scope_id,
                "scene_id": scene_id,
                "status": "open",
                "conversation_revision": 0,
                "created_at_ns": now_ns,
                "updated_at_ns": now_ns,
                "authority": deepcopy(authority),
                "participants": deepcopy(participants),
                "participant_ids": participant_ids,
                "actor_runtimes": runtimes,
                "events": [],
                "activations": {},
                "publications": [],
                "pending_resolutions": [],
                "memory_candidates": [],
                "audience_decision_ids": [],
                "idempotency": {},
                "open_idempotency_key": idempotency_key,
                "open_fingerprint": open_fingerprint,
            }
            self._write(session)
            return self.public_status(session)

    @staticmethod
    def public_status(session: dict[str, Any]) -> dict[str, Any]:
        return {
            key: deepcopy(session[key])
            for key in (
                "schema_version",
                "contract",
                "conversation_id",
                "campaign_id",
                "branch_id",
                "scope_id",
                "scene_id",
                "status",
                "created_at_ns",
                "updated_at_ns",
                "conversation_revision",
                "authority",
                "participants",
            )
        } | {
            "cursor": len(session["events"]),
            "pending_activation_count": sum(
                item.get("status") in {"pending", "claimed"}
                for item in session["activations"].values()
            ),
            "publication_count": len(session["publications"]),
            "actor_runtimes": [
                {
                    key: deepcopy(runtime[key])
                    for key in (
                        "actor_runtime_id",
                        "actor_id",
                        "status",
                        "inbox_cursor",
                        "working_state_revision",
                    )
                }
                for runtime in session["actor_runtimes"].values()
            ],
        }

    def require_owner(
        self, conversation_id: str, *, campaign_id: str, principal_id: str
    ) -> dict[str, Any]:
        session = self.get(conversation_id)
        if session.get("campaign_id") != campaign_id:
            raise ValueError("conversation belongs to another campaign")
        if session.get("principal_id") != principal_id:
            raise PermissionError("conversation belongs to another principal")
        return session

    def append_event(
        self,
        session: dict[str, Any],
        *,
        event: dict[str, Any],
        audience_facts: dict[str, Any],
        expected_revision: int,
        idempotency_key: str,
    ) -> dict[str, Any]:
        session, replay = self.begin_mutation(
            str(session["conversation_id"]),
            expected_revision=expected_revision,
            idempotency_key=idempotency_key,
            operation="ingest",
            payload={"event": event, "audience_facts": audience_facts},
        )
        if replay is not None:
            return replay
        if session["status"] != "open":
            raise ValueError(f"conversation is not open: {session['status']}")
        if len(session["events"]) >= MAX_CONVERSATION_EVENTS:
            raise ValueError("conversation has reached its 200-event limit; close or abort it")
        decision_id = str(audience_facts["decision_id"])
        if decision_id in session["audience_decision_ids"]:
            raise ValueError("audience_facts.decision_id must be unique in the conversation")
        sequence = len(session["events"]) + 1
        saved = {
            "event_id": f"conversation-event:{session['conversation_id']}:{sequence}",
            "sequence": sequence,
            **deepcopy(event),
            "audience_facts": deepcopy(audience_facts),
        }
        saved["actor_inboxes"] = self._actor_inboxes(saved, audience_facts)
        resolved_ids = list(event.get("resolved_resolution_ids") or [])
        resolutions_by_id = {
            str(item["resolution_id"]): item for item in session["pending_resolutions"]
        }
        if unknown := sorted(set(resolved_ids) - set(resolutions_by_id)):
            raise ValueError(f"resolution event cites unknown resolution ids: {unknown}")
        for resolution_id in resolved_ids:
            resolution = resolutions_by_id[str(resolution_id)]
            if resolution.get("status") != "pending":
                raise ValueError(f"resolution is already settled: {resolution_id}")
            resolution["status"] = "resolved"
            resolution["resolution_event_id"] = saved["event_id"]
        session["events"].append(saved)
        session["audience_decision_ids"].append(decision_id)
        if saved["type"] == "speech":
            self._append_heard_statement_candidates(
                session,
                event=saved,
                segments=[{"text": str(saved.get("content") or "")}],
                segment_audience_facts=[audience_facts],
            )
        resolved_activation_ids = {
            str(resolutions_by_id[resolution_id].get("activation_id") or "")
            for resolution_id in resolved_ids
        }
        for candidate in session.get("memory_candidates") or []:
            if (
                str(candidate.get("source_activation_id") or "")
                in resolved_activation_ids
                and not candidate.get("source_event_id")
            ):
                candidate["source_event_id"] = saved["event_id"]
        activations = []
        for actor_id in audience_facts["response_actor_ids"]:
            runtime = session["actor_runtimes"].get(actor_id)
            if runtime is None:
                continue
            activation_id = str(uuid4())
            activation = {
                "activation_id": activation_id,
                "actor_runtime_id": runtime["actor_runtime_id"],
                "actor_id": actor_id,
                "reason": "agent_selected_response",
                "response_required": True,
                "from_cursor": runtime["inbox_cursor"],
                "to_cursor": sequence,
                "status": "pending",
                "lease": None,
            }
            session["activations"][activation_id] = activation
            activations.append(self._public_activation(session, activation))
        public_event = {
            key: deepcopy(value) for key, value in saved.items() if key != "actor_inboxes"
        }
        return self.finish_mutation(
            session,
            {
                "conversation_id": session["conversation_id"],
                "event": public_event,
                "activations": activations,
            },
        )

    @staticmethod
    def _actor_inboxes(event: dict[str, Any], audience: dict[str, Any]) -> dict[str, Any]:
        understood = set(audience["understood_actor_ids"])
        partial = dict(audience["partial_renditions"])
        result: dict[str, Any] = {}
        for actor_id in audience["perceived_actor_ids"]:
            base = {
                "event_id": event["event_id"],
                "sequence": event["sequence"],
                "type": event["type"],
                "speaker_actor_id": event.get("speaker_actor_id", ""),
                "language": event.get("language", ""),
                "delivery": event.get("delivery", ""),
                "audience_decision_id": audience["decision_id"],
            }
            if actor_id in understood:
                base.update(
                    {
                        "comprehension": "full",
                        "content": event.get("content", ""),
                        "utterance_segments": deepcopy(event.get("utterance_segments") or []),
                        "visible_cues": deepcopy(event.get("visible_cues") or []),
                        "visible_action": event.get("visible_action", ""),
                        "resolved_resolution_ids": list(event.get("resolved_resolution_ids") or []),
                    }
                )
            elif actor_id in partial:
                base.update({"comprehension": "partial", "content": partial[actor_id]})
            else:
                base.update(
                    {
                        "comprehension": "perceived_only",
                        "content": (
                            "A communication or action was perceived, but its content "
                            "was not understood."
                        ),
                    }
                )
            result[actor_id] = base
        return result

    def _capability(self, session: dict[str, Any], activation: dict[str, Any]) -> str:
        message = ":".join(
            (
                str(session["conversation_id"]),
                str(activation["activation_id"]),
                str(activation["actor_runtime_id"]),
                str(session["principal_id"]),
            )
        )
        return hmac.new(self._secret, message.encode("utf-8"), hashlib.sha256).hexdigest()

    def _public_activation(
        self, session: dict[str, Any], activation: dict[str, Any]
    ) -> dict[str, Any]:
        return {
            key: deepcopy(activation[key])
            for key in (
                "actor_id",
                "reason",
                "response_required",
                "from_cursor",
                "to_cursor",
                "status",
            )
        } | {
            "activation_ref": self._capability(session, activation),
            "conversation_revision": int(session["conversation_revision"])
            + (1 if session.get("_pending_mutation") else 0),
        }

    def _activation_from_ref(self, session: dict[str, Any], activation_ref: str) -> dict[str, Any]:
        for activation in session["activations"].values():
            if secrets.compare_digest(activation_ref, self._capability(session, activation)):
                return activation
        raise PermissionError("invalid actor-scoped activation_ref")

    def list_activations(self, session: dict[str, Any]) -> list[dict[str, Any]]:
        return [
            self._public_activation(session, item)
            for item in session["activations"].values()
            if item["status"] in {"pending", "claimed"}
        ]

    def checkout(
        self,
        session: dict[str, Any],
        *,
        activation_ref: str,
        cursor: int,
        include_bootstrap: bool,
        expected_revision: int,
        idempotency_key: str,
    ) -> dict[str, Any]:
        session, replay = self.begin_mutation(
            str(session["conversation_id"]),
            expected_revision=expected_revision,
            idempotency_key=idempotency_key,
            operation="claim_activation",
            payload={
                "activation_ref": activation_ref,
                "cursor": cursor,
                "include_bootstrap": include_bootstrap,
            },
        )
        if replay is not None:
            return replay
        activation = self._activation_from_ref(session, activation_ref)
        if activation["status"] == "completed":
            raise ValueError("activation is already completed")
        now_ns = time.time_ns()
        current_lease = activation.get("lease")
        if current_lease and int(current_lease.get("expires_at_ns", 0)) > now_ns:
            raise ValueError("activation is already leased; replay the original idempotency_key")
        lease_id = str(uuid4())
        expires_at_ns = now_ns + self.lease_ttl_s * 1_000_000_000
        activation["lease"] = {"lease_id": lease_id, "expires_at_ns": expires_at_ns}
        activation["status"] = "claimed"
        runtime = session["actor_runtimes"][activation["actor_id"]]
        inbox = [
            deepcopy(event)
            for event in session["events"]
            for item in [dict(event.get("actor_inboxes") or {}).get(activation["actor_id"])]
            if item is not None and int(event["sequence"]) > max(0, int(cursor))
        ]
        event_basis_refs = [str(item["event_id"]) for item in inbox]
        context = runtime["context"]
        allowed_basis_refs = sorted(
            {
                *(str(item) for item in context["constraints"]["allowed_basis_refs"]),
                *event_basis_refs,
            }
        )
        capsule = {
            "schema_version": NPC_CONVERSATION_SCHEMA_VERSION,
            "contract": NPC_CONVERSATION_CONTRACT,
            "conversation_id": session["conversation_id"],
            "activation_id": activation["activation_id"],
            "actor_runtime_id": activation["actor_runtime_id"],
            "actor_id": activation["actor_id"],
            "lease_id": lease_id,
            "lease_expires_at_ns": expires_at_ns,
            "context_manifest": {
                "campaign_id": session["campaign_id"],
                "branch_id": session["branch_id"],
                "actor_revision": context["authority"]["actor_revision"],
                "working_state_revision": runtime["working_state_revision"],
                "inbox_cursor": len(session["events"]),
                "conversation_revision": int(session["conversation_revision"]) + 1,
            },
            "bootstrap": deepcopy(context) if include_bootstrap else None,
            "working_state": deepcopy(runtime["working_state"]),
            "inbox": inbox,
            "constraints": {
                "allowed_basis_refs": allowed_basis_refs,
                "allowed_target_actor_ids": list(session["participant_ids"]),
                "may_call_tools": False,
                "may_roll_dice": False,
                "may_write_state": False,
                "output_contract": "npc-conversation-proposal.v4",
            },
        }
        return self.finish_mutation(session, capsule)

    def submit(
        self,
        session: dict[str, Any],
        *,
        activation_ref: str,
        lease_id: str,
        proposal: Any,
        expected_revision: int,
        idempotency_key: str,
    ) -> dict[str, Any]:
        session, replay = self.begin_mutation(
            str(session["conversation_id"]),
            expected_revision=expected_revision,
            idempotency_key=idempotency_key,
            operation="submit_proposal",
            payload={"activation_ref": activation_ref, "lease_id": lease_id, "proposal": proposal},
        )
        if replay is not None:
            return replay
        activation = self._activation_from_ref(session, activation_ref)
        lease = activation.get("lease") or {}
        if lease.get("lease_id") != lease_id:
            raise PermissionError("invalid activation lease")
        if int(lease.get("expires_at_ns", 0)) <= time.time_ns():
            raise ValueError("activation lease expired")
        runtime = session["actor_runtimes"][activation["actor_id"]]
        allowed_basis_refs = {
            *(str(item) for item in runtime["context"]["constraints"]["allowed_basis_refs"]),
            *(
                str(event["event_id"])
                for event in session["events"]
                if activation["actor_id"] in dict(event.get("actor_inboxes") or {})
            ),
        }
        try:
            normalized = normalize_conversation_proposal(proposal)
            validate_conversation_proposal(
                normalized,
                conversation_id=session["conversation_id"],
                activation_id=activation["activation_id"],
                actor_runtime_id=activation["actor_runtime_id"],
                actor_id=activation["actor_id"],
                allowed_basis_refs=allowed_basis_refs,
                allowed_actor_ids=set(session["participant_ids"]),
            )
        except ValueError as exc:
            return {
                "status": "validation_failed",
                "validation_issues": [{"path": "proposal", "message": str(exc)}],
                "lease_retained": True,
                "conversation_revision": int(session["conversation_revision"]),
            }
        proposal = normalized
        if not proposal["response_bid"]["should_respond"]:
            activation["status"] = "completed"
            activation["lease"] = None
            runtime["inbox_cursor"] = len(session["events"])
            return self.finish_mutation(
                session, {"status": "passed", "publication": None, "resolution_requests": []}
            )

        publication = None
        if (
            proposal["utterance_segments"]
            or proposal["visible_cues"]
            or proposal["proposed_action"]["summary"]
        ):
            publication = derive_publication(proposal, publication_id=str(uuid4()))
            if proposal["proposed_action"]["settlement"] == "mechanical":
                publication["visible_action"] = ""
                publication["action_pending_resolution"] = True
            publication["status"] = "pending_audience"
            publication["speaker_actor_id"] = activation["actor_id"]
            session["publications"].append(deepcopy(publication))
        created_resolutions = [
            {
                **deepcopy(item),
                "resolution_id": str(uuid4()),
                "activation_id": activation["activation_id"],
                "actor_id": activation["actor_id"],
                "status": "pending",
            }
            for item in proposal["resolution_requests"]
        ]
        session["pending_resolutions"].extend(created_resolutions)
        proposed_candidates = []
        for kind, values in proposal["working_deltas"].items():
            candidate_kind = "commitment" if kind == "commitments" else kind.removesuffix("s")
            for value in values:
                proposed_candidates.append(
                    self._append_memory_candidate(
                        session,
                        actor_id=str(activation["actor_id"]),
                        kind=candidate_kind,
                        value=value,
                        source_activation_id=str(activation["activation_id"]),
                        source_publication_id=(
                            str(publication["publication_id"]) if publication else ""
                        ),
                    )
                )
        if proposed_candidates:
            for kind, values in proposal["working_deltas"].items():
                runtime["working_state"][kind].extend(deepcopy(values))
            runtime["working_state_revision"] += 1
        runtime["inbox_cursor"] = len(session["events"])
        activation["status"] = "completed"
        activation["lease"] = None
        return self.finish_mutation(
            session,
            {
                "status": "publication_ready" if publication else "resolution_required",
                "publication": publication,
                "resolution_requests": deepcopy(created_resolutions),
                "memory_candidate_ids": [
                    str(item["candidate_id"]) for item in proposed_candidates
                ],
            },
        )

    def cancel_activation(
        self,
        session: dict[str, Any],
        *,
        activation_ref: str,
        lease_id: str,
        expected_revision: int,
        idempotency_key: str,
    ) -> dict[str, Any]:
        session, replay = self.begin_mutation(
            str(session["conversation_id"]),
            expected_revision=expected_revision,
            idempotency_key=idempotency_key,
            operation="cancel_activation",
            payload={"activation_ref": activation_ref, "lease_id": lease_id},
        )
        if replay is not None:
            return replay
        activation = self._activation_from_ref(session, activation_ref)
        lease = dict(activation.get("lease") or {})
        if lease.get("lease_id") != lease_id:
            raise PermissionError("invalid activation lease")
        activation["lease"] = None
        activation["status"] = "pending"
        return self.finish_mutation(session, {"status": "pending", "cancelled": True})

    def publish(
        self,
        session: dict[str, Any],
        *,
        publication_id: str,
        audience_facts: dict[str, Any],
        segment_audience_facts: list[dict[str, Any]] | None,
        expected_revision: int,
        idempotency_key: str,
    ) -> dict[str, Any]:
        session, replay = self.begin_mutation(
            str(session["conversation_id"]),
            expected_revision=expected_revision,
            idempotency_key=idempotency_key,
            operation="publish",
            payload={
                "publication_id": publication_id,
                "audience_facts": audience_facts,
                "segment_audience_facts": segment_audience_facts or [],
            },
        )
        if replay is not None:
            return replay
        publication = next(
            (item for item in session["publications"] if item["publication_id"] == publication_id),
            None,
        )
        if publication is None:
            raise LookupError(publication_id)
        if publication.get("status") != "pending_audience":
            raise ValueError("publication is not awaiting audience facts")
        if len(session["events"]) >= MAX_CONVERSATION_EVENTS:
            raise ValueError("conversation has reached its 200-event limit; close or abort it")
        segments = list(publication.get("utterance_segments") or [])
        segment_facts = list(segment_audience_facts or [])
        if segment_facts and len(segment_facts) != len(segments):
            raise ValueError("segment_audience_facts must contain one ruling per utterance segment")
        if not segment_facts:
            segment_facts = [deepcopy(audience_facts) for _ in segments]
        elif (
            len(
                {
                    str(audience_facts["decision_id"]),
                    *(str(item["decision_id"]) for item in segment_facts),
                }
            )
            != len(segment_facts) + 1
        ):
            raise ValueError(
                "explicit overall and segment audience facts must use distinct decision ids"
            )
        decision_ids = {
            str(audience_facts["decision_id"]),
            *(str(item["decision_id"]) for item in segment_facts),
        }
        if decision_ids & set(session["audience_decision_ids"]):
            raise ValueError("audience decision ids must be unique in the conversation")
        sequence = len(session["events"]) + 1
        event = {
            "event_id": f"conversation-event:{session['conversation_id']}:{sequence}",
            "sequence": sequence,
            "type": "npc_publication",
            "speaker_actor_id": publication["speaker_actor_id"],
            "publication_id": publication_id,
            "content": publication["speech"],
            "utterance_segments": deepcopy(publication["utterance_segments"]),
            "visible_cues": deepcopy(publication["visible_cues"]),
            "visible_action": publication["visible_action"],
            "audience_facts": deepcopy(audience_facts),
            "segment_audience_facts": deepcopy(segment_facts),
        }
        event["actor_inboxes"] = self._publication_actor_inboxes(
            event, audience_facts, segment_facts
        )
        session["events"].append(event)
        session["audience_decision_ids"].extend(sorted(decision_ids))
        publication["status"] = "published"
        publication["audience_decision_id"] = str(audience_facts["decision_id"])
        self._append_heard_statement_candidates(
            session,
            event=event,
            segments=segments,
            segment_audience_facts=segment_facts,
            source_publication_id=publication_id,
        )
        for candidate in session.get("memory_candidates") or []:
            if str(candidate.get("source_publication_id") or "") == publication_id:
                candidate["source_event_id"] = event["event_id"]
        activations = []
        response_ids = {
            *audience_facts["response_actor_ids"],
            *(actor_id for facts in segment_facts for actor_id in facts["response_actor_ids"]),
        }
        for actor_id in sorted(response_ids):
            if actor_id == publication["speaker_actor_id"]:
                continue
            runtime = session["actor_runtimes"].get(actor_id)
            if runtime is None:
                continue
            activation = {
                "activation_id": str(uuid4()),
                "actor_runtime_id": runtime["actor_runtime_id"],
                "actor_id": actor_id,
                "reason": "agent_selected_response",
                "response_required": True,
                "from_cursor": runtime["inbox_cursor"],
                "to_cursor": sequence,
                "status": "pending",
                "lease": None,
            }
            session["activations"][activation["activation_id"]] = activation
            activations.append(self._public_activation(session, activation))
        public_event = {
            key: deepcopy(value) for key, value in event.items() if key != "actor_inboxes"
        }
        return self.finish_mutation(
            session,
            {
                "status": "published",
                "publication": {
                    key: deepcopy(value)
                    for key, value in publication.items()
                    if key not in {"speaker_actor_id", "status"}
                },
                "event": public_event,
                "activations": activations,
            },
        )

    @staticmethod
    def _publication_actor_inboxes(
        event: dict[str, Any],
        overall: dict[str, Any],
        segment_facts: list[dict[str, Any]],
    ) -> dict[str, Any]:
        perceived = {
            *overall["perceived_actor_ids"],
            *(actor_id for facts in segment_facts for actor_id in facts["perceived_actor_ids"]),
        }
        result: dict[str, Any] = {}
        for actor_id in perceived:
            rendered_segments = []
            for segment, facts in zip(
                event.get("utterance_segments") or [], segment_facts, strict=True
            ):
                if actor_id in facts["understood_actor_ids"]:
                    rendered_segments.append({**deepcopy(segment), "comprehension": "full"})
                elif actor_id in facts["partial_renditions"]:
                    rendered_segments.append(
                        {
                            "text": facts["partial_renditions"][actor_id],
                            "comprehension": "partial",
                            "audience_decision_id": facts["decision_id"],
                        }
                    )
                elif actor_id in facts["perceived_actor_ids"]:
                    rendered_segments.append(
                        {
                            "text": "Speech was perceived, but its content was not understood.",
                            "comprehension": "perceived_only",
                            "audience_decision_id": facts["decision_id"],
                        }
                    )
            result[actor_id] = {
                "event_id": event["event_id"],
                "sequence": event["sequence"],
                "type": event["type"],
                "speaker_actor_id": event["speaker_actor_id"],
                "utterance_segments": rendered_segments,
                "visible_cues": (
                    deepcopy(event.get("visible_cues") or [])
                    if actor_id in overall["perceived_actor_ids"]
                    else []
                ),
                "visible_action": (
                    event.get("visible_action", "")
                    if actor_id in overall["understood_actor_ids"]
                    else ""
                ),
                "audience_decision_id": overall["decision_id"],
            }
        return result
