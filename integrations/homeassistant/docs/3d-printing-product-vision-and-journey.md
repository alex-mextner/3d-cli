# Conversation-to-object: product vision and customer journey

Version 0.1 · 19 September 2026 · Proposed product direction, not a shipped platform specification

## 1. Product intent

A Lovable-like experience for physical 3D-printed objects: describe a need, provide a reference or an existing model, refine the result, approve a manufacturable version and its price, and follow it through production to receipt. The product should make the entire journey understandable and traceable rather than merely place a chat interface in front of a slicer.

**Initial operating model:** Ultra is the operator and fulfils requests using owned equipment, starting with the U1. Customer and operator can initially be the same person. Record self-use jobs as real production attempts so that the workflow can be learned without building a marketplace first.

**Future operating model:** a geographically distributed partner network can fulfil the same versioned order contract. Partner onboarding, routing, quality assurance, payments and logistics are future capabilities, not part of the camera-inspector deployment.

**Product promise:** an approved, useful physical result with a clear price, progress and recovery path. A generated model, a sliced file and a completed print are intermediate outputs, not the final outcome.

## 2. Experience principles

Keep one project workspace with a conversation, a versioned 3D preview, a specification, a quote and an order timeline. Use a guided next action at every state. Do not require the customer to understand G-code or slicer terminology; provide technical detail on demand for the operator.

Preserve the distinction between customer intent, AI proposals, approved artifacts and observed facts. A visually plausible model is not automatically dimensionally correct or safe for its intended application. No image-derived measurement or generated feature becomes an approved requirement without an explicit basis.

A change to dimensions, material, model revision, printer profile or quantity may invalidate the slice and quote. Show what changed and request approval again rather than silently manufacture a different object.

Unknown information remains unknown. Quotes can be estimates; live operation labels can be estimates; delivery dates can be tentative. Communicate those distinctions at the point where a decision is made.

## 3. End-to-end journey map

| Stage | Customer goal / concern | Customer-facing interaction | Operator / system work | Durable output and exit condition |
|---|---|---|---|---|
| 1. Describe the need | “Can you make this, and will it solve my problem?” | Text, photos, sketch, existing model or example; purpose, quantity and deadline. | Identify missing constraints, units, intended use and special review needs. Do not invent measurements from a photo. | Request with attachments and a list of confirmed versus missing requirements. |
| 2. Establish feasibility | “Is this a reasonable thing to print?” | Explain likely material/process options, uncertainties and possible alternatives. | Check build envelope, target fit, strength direction, finish, environment, assembly and realistic operating capability. | Specification draft and a feasibility decision; a blocked request has a reason and a next action. |
| 3. Obtain a model | “I have nothing / I have a file / this file needs changes.” | Three explicit paths: create, inspect an existing model, or modify a selected revision. | Use dimension-driven CAD for functional geometry and an appropriate mesh workflow for sculptural geometry. Keep units, provenance and source assets. | Model revision linked to the request and specification; previous revisions stay recoverable. |
| 4. Refine and approve | “Does this look right and fit where it belongs?” | Rotatable preview, important dimensions, version comparison and requested changes in the conversation. | Check topology, minimum features, clearances, assembly and orientation-dependent properties. Flag unsupported requirements for operator review. | Explicit approval of the model revision and specification. Rejection returns to stage 3 without losing history. |
| 5. Prepare production | “How will this be made?” | Material, colour, finish and speed/quality choices expressed in outcome language. | Select a validated machine/material/profile combination, orientation, supports, infill, ironing and tool mapping. Slice with a recorded slicer version and profile hash. | Immutable SliceArtifact tied to the exact model and manufacturing profile. Inspect the resulting toolpath before release. |
| 6. Estimate and confirm price | “What will I pay, and what is included?” | Preliminary range before slicing; revised quote after slicing, with inclusions, exclusions and lead-time assumptions. | Estimate material including supports/purge, machine time, preparation, design, postprocessing, failed-attempt allowance and fulfilment. | QuoteRevision referencing its model, slice, quantity, rate-card version and validity. No invented production rates. |
| 7. Commit the order | “Am I approving exactly what will be produced?” | One summary: version, dimensions, material, quantity, total, expected readiness and destination/collection. | Confirm approvals and resource availability; payment or self-use approval follows the selected operating model. | Approved order and approved production package. A revised price or artifact requires renewed approval. |
| 8. Queue and prepare | “When will it start?” | Queue position or a readiness window; notifications for material or scheduling problems. | Confirm stock, dry/material condition where relevant, correct plate, loaded heads, physical tool mapping and machine readiness. | ProductionAttempt assigned to equipment; operator preflight and an explicit start authorisation. |
| 9. Manufacture | “Is it progressing normally?” | Order-scoped camera, progress, current layer, plain-language operation, freshness and an estimated completion time. | Observe telemetry and G-code annotations; handle pause, runout, failed print and power recovery. Preserve the distinction between observed firmware state and inferred operation. | Timestamped production events linked to the attempt and artifact. A printer's `complete` state moves the attempt to inspection, not the order to delivered. |
| 10. Inspect and finish | “Is the part actually acceptable?” | Quality-check progress and evidence; approval required for any accepted deviation. | Remove supports, clean/postprocess, check count and dimensions/fit against the approved specification, attach photos and record defects. | QCReport: pass, rework, reprint or reject. Reprint creates a new attempt under the same order. |
| 11. Pack and fulfil | “How and when do I get it?” | Collection instructions or shipment tracking; contact details only where needed. | Pack, label, hand over and record carrier/pickup evidence. The operator owns the handoff until fulfilment is confirmed. | Fulfilment record with handoff, tracking or collection confirmation. |
| 12. Receive, validate and iterate | “Does it work in the real world?” | Confirm receipt and fit; request another copy, report a problem or ask for an improvement. | Route damage, dimensional mismatch and changed intent to different recovery paths. Compare actual cost/time with the quote. | Receipt and outcome record; repeat order references an approved revision, while an improvement creates a new revision. |

## 4. Recovery paths are part of the journey

| Situation | Required behaviour |
|---|---|
| A photograph has no trustworthy scale | Ask for a measurement or a reference dimension; do not silently generate a supposedly precise part. |
| A model cannot be manufactured as supplied | Explain the failed check and propose a model/process change. Do not alter an approved artifact invisibly. |
| The quote exceeds the budget | Compare material, size, finish, quantity and schedule alternatives. A cheaper proposal receives its own revision and approval. |
| A different printer or material must be used | Validate capability, re-slice for the target profile, compare outcome and quote, then obtain approval when the contract changes. Never assume G-code is portable. |
| The printer pauses or loses connectivity | Separate a confirmed machine pause from absent telemetry. Show last-observed timestamps and the responsible next action. |
| The attempt fails | Keep the failed attempt and evidence. Open a new attempt; do not overwrite history or charge silently for retries. |
| QC fails despite a completed print | Route to rework/reprint or customer-approved deviation. A `complete` printer event never bypasses QC. |
| Delivery fails or the item is damaged | Keep production acceptance and fulfilment claims separate; record evidence and the replacement/refund decision. |
| The received part does not fit | Compare approved dimensions, manufactured measurements and new requirements before assigning a cause. |

## 5. Domain model and traceability

Use a stable chain rather than a collection of unrelated files:

`Request → SpecRevision → ModelRevision → SliceArtifact → QuoteRevision → Order → ProductionAttempt → QCReport → Fulfilment → Receipt`

A Request includes the conversation and references. A SpecRevision records confirmed intent, dimensions, units, tolerances and acceptance criteria. A ModelRevision stores source CAD/mesh and its preview, derivation and approvals. A SliceArtifact stores the exact model hash, slicer/version, printer/material profile hashes, tool mapping, output G-code hash and estimates.

A QuoteRevision records the cost basis, price, assumptions, validity and approved scope. An Order links approved revisions and quantity; it must never silently resolve “latest model.” A ProductionAttempt records one real execution, including start authorisation, device, material, offsets/tool mapping, start/end timestamps and outcome. Several attempts may fulfil one order. An order may also contain multiple parts or plates, so introduce a production-job grouping when that case is actually needed.

QCReport, Fulfilment and Receipt are independent records. Customer satisfaction and confirmed delivery should not be inferred from machine telemetry.

**Two identities are necessary:** a G-code content hash identifies the artifact; a ProductionAttempt ID identifies one execution. Printing the same file twice must create two attempts. The currently deployed inspector has a G-code fingerprint but is not yet an order or attempt registry.

## 6. State ownership and architecture

The product backend should own requests, approvals, artifacts, quotes, orders, attempts and fulfilment. Home Assistant is the current equipment adapter and operator surface, not the future order database.

A proposed application structure is:

`Project workspace → workflow/state service → model and slicing workers → production adapter → U1 / future partner equipment`

The production adapter reports normalised events and observations, including device ID, attempt ID, artifact hash, event/observation timestamp, source, confidence and freshness. Idempotency keys and source sequence identifiers prevent repeated webhooks or reconnects from duplicating transitions. Out-of-order device observations must not regress a finished order.

Use background workers with bounded jobs for CAD generation, geometry checking, slicing and quoting. Do not execute arbitrary shell commands or G-code produced by an untrusted conversation. Validate structured parameters, run analysis/slicing in an isolated environment, preserve artifacts, and require operator release before physical printing.

Keep failure handling deterministic. AI can explain a failure and propose revisions; workflow transitions, quotes, toolpaths and equipment permissions must have explicit contracts. The first version does not need a distributed architecture merely because a partner network is planned.

## 7. Quote model — proposed, not calibrated

The initial cost model should expose its basis:

`estimated production cost = model/preparation labour + material + machine occupancy + energy (when not already included) + consumables + postprocessing/QC + packing + expected rework allowance`

`quoted price = estimated production cost + commercial margin + fulfilment charges + applicable charges determined for the transaction`

Avoid double counting energy or maintenance if the machine-hour rate includes them. Allocate design work separately from repeat-unit production. A risk allowance is an estimate, not a promise of failure or a hidden fee.

Before an approved model and slice exist, display a range with assumptions. After slicing, calculate a revision-specific estimate from real material and time outputs, then compare with actual consumption, duration and operator effort. A partner quote must include that partner's verified capacity, rate card and fulfilment assumptions rather than simply reuse Ultra's home-machine rates.

No actual currency amounts or cost accuracy targets are specified here: the rate card and measured baselines do not yet exist. Payment, cancellation, tax, invoicing, consumer rights and cross-border shipping rules require jurisdiction-specific implementation before external sales; this document does not establish those rules.

## 8. Partner network, introduced in stages

Start with a small invited pilot after the owned-equipment journey works. Each partner needs a capability profile: location/service region, supported processes, machine/material/profile combinations, build envelope, capacity, expected readiness, finishing options and the evidence needed for acceptance. Record profile versions and qualification results.

Route a production package to eligible candidates; optimise for required outcome, schedule, cost and fulfilment constraints without hiding compromises. Initially keep the assignment decision explicit. Validate and re-slice for the actual equipment rather than forwarding an arbitrary G-code file between devices.

Partner isolation is mandatory: a partner sees only assigned work; a customer sees only their order. Camera and telemetry permissions must be scoped to an order/attempt, expire, be revocable and stop when that attempt ends. **The currently installed camera links are camera-scoped until expiry, not order-scoped; they are not sufficient for a multi-customer network.** A still-valid link could otherwise show a subsequent print.

Add partner settlement, quality disputes, reprint responsibility, fulfilment integration and geographic/legal coverage only with clear ownership and contracts. “Worldwide network” is a direction, not a claim of existing coverage.

## 9. Delivery sequence and evidence gates

| Stage | Scope | Evidence required to move on |
|---|---|---|
| A. Production observability | U1 camera sharing, freshness, current operation with provenance, per-layer G-code explanation. | Real device/API tests; expired links rejected; no false “ironing now” after completion; no printer-control changes. This is the implemented workstream. |
| B. Owned-equipment order journey | One project workspace, requirements, model revisions, manual upload/slice where necessary, explicit approval, attempts, QC and collection. | A real request is fulfilled end to end without reconstructing state from chat or filenames. |
| C. Assisted model and production preparation | Bounded model creation/modification, automated geometry checks, reproducible slicing, quote revisions and approval invalidation. | Repeatable artifacts, measured quote error, safe preflight and understandable failure recovery. |
| D. Invited partner pilot | Capability registry, scoped jobs/shares, assignment, partner QC and fulfilment evidence. | Multiple operators fulfil the same contract without data leakage or unrecorded substitutions. |
| E. Broader network | More regions, equipment types, settlement and logistics integrations. | Quality, unit economics, support ownership and jurisdiction-specific requirements validated in each expansion. |

Measure request-to-approved-spec time, model revision count, approved-model-to-quote time, quote error versus actual cost/time, first-attempt yield, operator touch time, QC escape rate, readiness/delivery adherence and outcome satisfaction. Define target values from measured baselines, not arbitrary percentages.

## 10. Current implementation boundary and next product slice

Installed now: a local U1 telemetry/G-code inspector, shared JPEG/MJPEG overlays, an HA per-layer analysis card and the existing expiring image links. It does not create a model, run a slicer, accept payment, calculate a calibrated price, dispatch to partners or arrange delivery.

The next product slice should be **one request becoming one accepted physical object on Ultra's own equipment**, with all decisions and artifacts linked. Automate the most repetitive or error-prone steps after observing that path. Do not start with a global marketplace UI while approval, failure recovery and QC remain implicit.

Open product decisions include the first job category (functional/parametric versus sculptural), accepted input formats, rate card, acceptance tolerances, storage/retention policy, pickup versus shipping and initial partner geography. These are explicit unknowns, not blockers to the observability work already deployed.
