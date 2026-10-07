# Curriculum notes: sourangshupal/langraph-tutorials

Source: https://github.com/sourangshupal/langraph-tutorials (fetched 2026-10-07, via WebFetch,
README-level listing only). LangGraph version referenced: 1.1.9. 10 notebooks, beginner to
"expert-level system design" per the repo's own description [claimed].

Per-notebook verdict for Mor (learn this to understand the ideas) and for the build (does Orca
need the equivalent capability):

1. Introduction to LangGraph (graph construction basics)
   ADOPT AS-IS for learning: read it, it is the vocabulary (node, edge, state) the rest of the
   field uses, useful even though Orca will not use LangGraph's object model.

2. Dynamic Routing with Conditional Edges
   ADAPT: the idea "next step depends on a value in state" is real and Orca needs it, but as a
   plain `if` in the step runner, not a conditional-edge object. Read for the idea, not the API.

3. Tool Integration and OpenAI Function Calling
   SKIP for the build: Orca's tool-calling is whatever the underlying model client already does
   (Claude/OpenAI tool use); nothing LangGraph-specific to port. Worth a skim for Mor only if
   function-calling itself is unfamiliar.

4. State Persistence and Memory
   ADAPT: this is the notebook closest to Orca's Run durability and Agent memory design. Read it
   closely, then implement the stdlib-only version (JSON checkpoint file, markdown memory tree)
   instead of LangGraph's checkpointer/store classes.

5. Human-in-the-Loop and Streaming
   ADAPT: the pause-for-approval pattern matters for a Supervisor that reports progress on a long
   run. Steal the pattern (pause at a step, resume on signal), not the implementation.

6. Parallel Execution and Subgraphs
   SKIP for the build (for now): Orca's day-one Supervisor caps fan-out and runs subtasks
   sequentially or with a small fixed worker pool (see engine-design.md); true subgraph composition
   is a later-milestone concern, not day one. Worth reading for Mor to know the ceiling exists.

7. Production Patterns (enterprise-grade optimization)
   SKIP: aimed at multi-tenant, high-scale deployments. Orca is a single-owner, single-host tool;
   this notebook's concerns (scaling checkpointer backends, etc.) are out of scope by design.

8. Command Primitive, Functional API & Long-Term Memory
   ADAPT: "long-term memory across sessions" is exactly Orca's per-agent git memory tree. Read for
   the problem framing, implement with files, not with LangGraph's memory store API.

9. Multi-Agent Systems
   ADAPT: directly relevant to the Supervisor + specialist agents shape. Read for the coordination
   problem (who decides, who executes, how results come back), reimplement in Orca's own terms.

10. Testing, Observability & LangGraph Platform
    SKIP the platform parts (that is LangGraph's hosted product, irrelevant here); ADAPT the
    testing/observability framing: a Run record with per-step model, cost, and log (see
    engine-design.md, Run record) is Orca's answer to "observability," no separate platform needed.

## Net read for Mor
Notebooks 1, 2, 4, 5, 8, 9 are the ones worth actually working through, because they teach ideas
Orca's engine will reimplement. 3, 6, 7, 10 are skippable or skim-only: either API-specific
(3) or scale/product concerns Orca does not have on day one (6, 7, 10's platform half).
