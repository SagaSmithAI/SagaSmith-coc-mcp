# NPC conversation memory

CoC uses one current durable conversation protocol: `npc-conversation.v3`.
There is no runtime compatibility path for retired conversation journals or
close payloads.

## Settlement

- Worker proposals use `npc-conversation-proposal.v4`. Proposed facts,
  ActorKnowledge, and commitments become immutable memory candidates with
  stable IDs; they are not authoritative writes.
- Every understood speech segment also creates a heard-statement
  ActorKnowledge candidate for each listener. This applies symmetrically to
  investigator speech and published NPC speech, and does not imply that the
  statement is true.
- `npc_conversation(close)` accepts only `accepted_candidate_ids`. It rejects
  pending activations, publications, or mechanic resolutions and commits the
  selected candidates with the public transcript in one continuity
  transaction.
- The committed event keeps a short summary for display and a bounded,
  first-class `retrieval_text` for recall. Actor context expands only transcript
  segments that the recorded audience facts say that actor understood.

## Freshness and state boundaries

Each NPC worker is locked to the current actor revision and the complete head
manifest of that actor's relationship, goal, commitment, and ActorKnowledge
records. A change refreshes only that NPC runtime, invalidating unfinished work
from the prior runtime. Branch checkout, snapshot restore, undo/redo, scene
progress, phase changes, Chase, and Combat remain blocked until the active
conversation is closed or aborted.

## Storage bounds

An active conversation is limited to 200 public events and a 4 MiB journal.
Close and abort replace the working journal with a compact terminal receipt and
a compressed exact idempotency result. Terminal receipts expire after 30 days;
retired protocol journals are deleted rather than loaded.
