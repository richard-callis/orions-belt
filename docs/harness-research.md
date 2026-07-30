# AI Agent Harness: Architectures and Patterns

Research survey of how leading agent frameworks build the orchestration layer,
with concrete recommendations for Orion's Belt (a local Flask/SQLite single-user
app combining **multi-agent chat rooms** with **tiered-approval task agents**).

Domain model we're building toward: **Chats · Agents · Tools** — tools belong to
agents; an agent runs its tools in any context (chat room, task, cron/trigger).

---

## 1. Multi-Agent Chat / Conversation Orchestration

The core design question is *who speaks next*. Frameworks cluster into four
turn-taking strategies: **round-robin**, **LLM-picks-next-speaker (selector)**,
**@mention/handoff-addressed**, and **manager/orchestrator**.

### Microsoft AutoGen (AgentChat) — richest group-chat model
A `Team` is a shared conversation: participants "take turns broadcasting messages
to all other members" (one shared log every agent sees).
- **`RoundRobinGroupChat`** — deterministic rotation.
- **`SelectorGroupChat`** — an LLM "selects the next speaker based on the shared
  context." Override hooks: `selector_func` (return the next speaker's name) and
  `candidate_func` (filter eligible speakers before the LLM chooses).
- **Loop-avoidance default:** "the team will not select the same speaker
  consecutively unless it is the only agent available" (`allow_repeated_speaker`
  defaults to `False`).
- Also supports FSM / explicit speaker-transition graphs.
- Docs: <https://microsoft.github.io/autogen/stable//user-guide/agentchat-user-guide/selector-group-chat.html>

AutoGen is the closest match to "a chat room where humans and multiple agents
converse" — a broadcast conversation with pluggable speaker selection.

### CrewAI — process-driven pipelines, not free chat
**Sequential** (ordered tasks, output→context) and **Hierarchical** (an
auto-injected `manager_llm` allocates tasks). Oriented to task pipelines, not
human-in-the-room chat. Known weakness: the hierarchical manager tends to run
tasks sequentially rather than route to the best agent.
- Docs: <https://docs.crewai.com/en/concepts/processes>

### LangGraph — supervisor vs. swarm
- **Supervisor** — central orchestrator delegates to sub-agents and collates
  results; simpler to debug, costs a "translation hop" per handoff.
- **Swarm** — agents hand off control via explicit handoff tools; "the system
  remembers the last-active agent so subsequent messages continue with it."
- Recommended default: start supervisor, graduate to swarm when latency proves
  it. <https://focused.io/lab/multi-agent-orchestration-in-langgraph-supervisor-vs-swarm-tradeoffs-and-architecture>

### OpenAI Swarm / Agents SDK — addressed handoffs
Swarm reduces coordination to **routines** + **handoffs**, is **stateless**
between calls (state travels in explicit context variables). The production
**Agents SDK** keeps **Agents / Handoffs / Guardrails** with a built-in runner.
Handoff = *addressed* turn-taking: control transfers to one named agent.
- <https://openai.github.io/openai-agents-python/> · <https://github.com/openai/swarm>

### Letta / MemGPT — stateful agents
Less about topology, more about each agent being **stateful and self-managing**
(see §4) — the opposite of Swarm's statelessness.
- <https://www.letta.com/blog/agent-memory/>

| Strategy | Example | Next speaker chosen by |
|---|---|---|
| Round-robin | AutoGen `RoundRobinGroupChat` | Fixed rotation |
| LLM selector | AutoGen `SelectorGroupChat` | LLM reads history, returns a name; no repeat by default |
| Manager/orchestrator | CrewAI hierarchical, LangGraph supervisor | Central agent delegates + synthesizes |
| Addressed handoff | OpenAI Swarm/SDK, LangGraph swarm | Current agent transfers to one named peer |
| @mention | Slack multi-agent bots | Human names the agent |

---

## 2. Agent-Response Triggering and Loop Avoidance

### What triggers a response
Dominant production pattern for human+agent channels: **explicit @mention
addressing + thread subscription**. "@mention required to start a conversation
by default… Once the bot has an active session in a thread, subsequent replies
don't require a mention." Agents "only respond when called."
- <https://dust.tt/blog/slack-ai-agents> · <https://vercel.com/kb/guide/how-to-build-an-ai-agent-for-slack-with-chat-sdk-and-ai-sdk>

### Avoiding infinite agent-to-agent loops — layered brakes
- **Max turns / max messages** (`max_turns`, `MaxMessageTermination(n)`).
- **Text-mention / sentinel termination** (`TERMINATE`, `is_termination_msg`).
- **Max consecutive auto-replies** (`max_consecutive_auto_reply`) — brakes two
  agents ping-ponging.
- **No-repeat-speaker default** — prevents self-reply.
- **Human-in-the-loop as circuit breaker** — a human turn resets counters.

Best practice: **combine** a hard turn cap (fail-safe) + a semantic termination
condition (normal exit) + a no-self-reply rule (stops oscillation). Anthropic:
prefer a bounded workflow before an open-ended agentic loop.
- <https://www.anthropic.com/engineering/building-effective-agents>

---

## 3. Tool-Use / Execution Loop

- **ReAct** — tight Thought→Action→Observation loop; adaptive but one LLM call
  per step (expensive) and short-term. Best for simple, few-tool tasks.
- **Plan-and-Execute** — plan upfront, execute sequentially, replan on failure;
  fewer calls, cheaper, inspectable, parallelizable. **Security advantage**: the
  plan can be reviewed before any action runs.
- **Plain tool-calling loop** (what SDKs ship) — "call tools, send results back,
  loop until done." Anthropic's canonical loop: **gather context, take action,
  verify work, repeat.**
- <https://arxiv.org/pdf/2509.08646> · <https://www.anthropic.com/engineering/building-effective-agents>

### Approval gates (directly relevant to tiered approval)
OpenAI SDK and LangGraph implement it almost identically:
- Tools declare `needs_approval` — `True`, or **an async function that decides
  per call** (inspect args → *tiered* policy). Runs surface pending approvals as
  interruptions; **run state serializes and resumes** after a decision.
- LangGraph `interrupt_on` gates any subset of tools; state is checkpointed so
  the run resumes after approve/reject/edit.

Shared architecture: **tools carry an approval policy → the loop pauses and emits
an interruption before a gated tool runs → run state is persisted → an
approve/reject decision resumes the same run.** A per-call decision function is
what makes *tiered* approval possible (auto-allow read, confirm write, block
delete).
- <https://openai.github.io/openai-agents-python/human_in_the_loop/> · <https://docs.langchain.com/oss/python/deepagents/human-in-the-loop>

---

## 4. Context & Memory Management

### Short-term: compaction
Three approaches — **LLM summarization** (risks detail loss), **verbatim**
(preserves detail), **relevance-based deletion**. Recommended: **anchored
hierarchical summarization** — recent turns verbatim, older compressed, with
progressively more compact summaries. Combine compaction + structured external
memory + sub-agent delegation. Claude Code auto-compacts at ~98% of the window.
- <https://zylos.ai/research/2026-04-21-agent-context-compaction-long-running-sessions/>

### Long-term: the MemGPT/Letta tiered model
OS-inspired hierarchy:
- **Core memory** — small always-in-context block (persona, key facts).
- **Recall memory** — full history outside context, searchable on demand.
- **Archival memory** — long-term external (vector) store, queried via tools.

Letta agents **self-edit** memory via function calls. Takeaway: **vector recall
is one tier, not the whole story.**
- <https://www.letta.com/blog/agent-memory/>

---

## 5. Streaming and Per-Conversation State

- **Streaming:** AutoGen's `run_stream` yields an async iterator of events
  (`message_start`, token deltas, `tool_call`, `message_end`) ending in a
  `TaskResult`. OpenAI SDK / LangGraph expose equivalent event streams.
- **State isolation:** state is scoped to a run/team/thread and serializable.
  AutoGen keeps context in the team (`save_state`/`load_state`); Swarm is
  stateless (context in explicit variables); OpenAI SDK/LangGraph use a
  serializable per-thread run state (also powers approval pause/resume).
- **Concurrency rule:** give each conversation its **own state object / id**,
  never a shared global.

---

## 6. Recommendations for Orion's Belt

Two orchestration modes over a shared **Agent/Tool/Memory core**. No heavyweight
framework needed — copy specific mechanisms.

**(a) Room agents converse** — model a room as a single shared message log; on a
new message, run a speaker-selection step, append replies, re-evaluate
(AutoGen's broadcast-then-select). Each agent turn is an LLM call: room prompt +
agent persona + recent (compacted) history + recall.

**(b) Speaker selection** — layer in priority: **@mention wins** → else an **LLM
selector picks from a filtered candidate set** → else **default to silence**
(wait for the human). Skip selection for single-agent rooms.

**(c) Avoid infinite loops** — enforce simultaneously: no-self-reply; a
**consecutive-agent-turn cap** (yield to human after N); a hard per-prompt
ceiling; a semantic "done" signal; and **the human message resets the counter**.

**(d) Isolation** — one state object per room, keyed by `room_id` in SQLite,
never a Flask global. Run long agent turns off the request thread; stream events
to the UI.

**(e) Task agents** — a bounded tool-calling loop ("gather/act/verify/repeat")
with a hard max-iteration cap; add a plan-then-execute front for multi-step
tasks so the plan is approvable. Implement **tiered approval as a per-call policy
function** per tool (auto-allow read, confirm write, block/confirm
delete) → pause, persist run state, surface a pending approval, resume.

**(f) Memory** — three tiers backed by SQLite: **core** (small per-agent block),
**recall** (message log, FTS5-searchable), **archival** (vector table via a
tool); anchored hierarchical **compaction** at a high-water mark.

### Bottom line
Replicate four mechanisms: **(1)** broadcast-log + candidate/selector speaker
choice (`allow_repeated_speaker=False`); **(2)** layered termination brakes;
**(3)** pause-serialize-resume approval gate driven by a per-tool tiered policy;
**(4)** MemGPT's three-tier self-editing memory. Keep all state keyed by
`room_id`/`run_id` in SQLite, run agent turns off the request thread, stream
events to the UI. Start with the simplest workflow; add autonomy only where the
task demands it.

---

## How this maps to what Orion's Belt does today

| Recommendation | Status in Orion's Belt |
|---|---|
| Broadcast message log per room | ✅ `chat_room_messages` keyed by `room_id` |
| @mention speaker selection | ✅ `@mention` targets specific agents |
| LLM selector for next speaker | ⬜ not yet (round-robin/all-reply today) |
| No-self-reply + consecutive-turn cap | ✅ no immediate self-reply + admin-set cap (`agents.max_agent_turns`) |
| Only human messages start a burst | ✅ agents can't self-trigger loops |
| Per-room state, off request thread | ✅ background thread keyed by `room_id` |
| Bounded task tool loop | ✅ `_execute_run` (max iterations) |
| Tiered approval, pause/serialize/resume | ✅ Tier-3 hard-stop → `PendingToolApproval` |
| Agent owns tools, runs anywhere | ✅ `AgentRuntime` (chat rooms; task/cron next) |
| Three-tier memory + compaction | 🟡 vector recall + basic compaction; no core/archival tiers yet |

### Primary sources
- AutoGen Selector Group Chat: <https://microsoft.github.io/autogen/stable//user-guide/agentchat-user-guide/selector-group-chat.html>
- AutoGen chat termination: <https://microsoft.github.io/autogen/0.2/docs/tutorial/chat-termination/>
- CrewAI Processes: <https://docs.crewai.com/en/concepts/processes>
- LangGraph supervisor/swarm: <https://focused.io/lab/multi-agent-orchestration-in-langgraph-supervisor-vs-swarm-tradeoffs-and-architecture>
- OpenAI Agents SDK / HITL: <https://openai.github.io/openai-agents-python/human_in_the_loop/>
- OpenAI Swarm: <https://github.com/openai/swarm>
- Letta / MemGPT memory: <https://www.letta.com/blog/agent-memory/>
- Anthropic, Building Effective Agents: <https://www.anthropic.com/engineering/building-effective-agents>
- ReAct vs Plan-and-Execute: <https://arxiv.org/pdf/2509.08646>
- Context compaction (2026): <https://zylos.ai/research/2026-04-21-agent-context-compaction-long-running-sessions/>
- Multi-agent Slack triggering: <https://dust.tt/blog/slack-ai-agents>
