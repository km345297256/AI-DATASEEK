# Frontend browser regression gate

`scripts/check-regressions.sh` now runs `npm run test:browser` after the frontend
production build. The gate requires all eight scenarios; missing browser engines
are errors, never skips. For a frontend-only check:

```sh
cd frontend
npm test
npm run type-check
npm run build
npm run test:browser
```

## Runtime installation

Use an installed Playwright module, or provide its absolute entry path through
`VISUALIZATION_PLAYWRIGHT_MODULE`. The existing Codex bundled runtime is also
discovered when available. Install Chromium and WebKit using the **same module's**
`cli.js` (`node /absolute/path/to/playwright/cli.js install chromium webkit`).
The gate does not download dependencies automatically. On Linux, install that
Playwright version's documented system dependencies as part of runner setup.

`PLAYWRIGHT_BROWSERS_PATH` may point to a dedicated cache or temporary directory;
use the same value for installation and verification. Chromium can use an
explicit `VISUALIZATION_BROWSER_EXECUTABLE`, or an existing macOS Chrome. WebKit
always uses Playwright's matching engine, never the Chromium override. To run a
single diagnostic scenario, set `DATASEEK_BROWSER_ENGINE=webkit` and execute
`node tests/browser/chat-layout-smoke.mjs` or
`node tests/browser/session-recovery-smoke.mjs`. This does not replace the matrix.

## Scope and evidence

- Chromium: chat layout, follow-up outcomes, task status/focus, session recovery,
  and shell console details.
- WebKit: chat layout/input/reading anchors, session recovery, and shell console details.
- Chat layout exercises the real ChatBox and ConversationViewport. OS IME
  automation is not claimed: real browser KeyboardEvents cover `isComposing`
  and Safari's keyCode 229, followed by a native ordinary Enter. History prepend
  and delayed image decode exercise the native ResizeObserver. As before, the
  unrelated ChatMessage in this layout fixture is a documented test substitute.
- Follow-up outcomes render real conversation/message/outcome cards.
- Session recovery uses the production Vue controller, API adapter, SSE parser,
  native Fetch and AbortController with synthetic intercepted HTTP responses.
  It covers pre-acknowledgement EOF retry, protocol failure, original-ID manual
  recovery, receipt-confirmed read-only replay, local echo/attachment handoff,
  retained newer draft, and late receipt/history responses after a session switch.
  It also covers stop acknowledgement followed by delayed outcome/attachments,
  an outcome-only EOF before terminal replay, and failed/uncertain stops. These
  recoveries use the original `observe_client_message_id` with no resubmission;
  a new send stays blocked until the durable terminal is observed, and the newer
  draft remains intact. Automatic and explicit recovery both stay read-only.
  Its minimal DOM is a state/transport harness, not a replacement visual snapshot
  of ChatPage or DatasetSeekPage.
- Shell console uses the real ShellToolView and built styles. It checks explicit
  zero/nonzero/negative/unknown exit status, suppresses invented status for older
  synthetic dataset/spill records, and covers escaping, sensitive display text,
  fallback messaging and unmount cleanup without backend access.

The recovery scenario expects exactly two existing `API request failed network`
console diagnostics from intentionally aborted receipt/history reads. Other
console errors and all uncaught page errors fail the scenario; the test does not
silence the production logger or treat arbitrary network failures as success.

Every scenario intercepts all requests and fails on unexpected or external
requests. It does not listen on a port, reach a running deployment, use a real
model, or access user datasets. JSON reports and screenshots go to newly created
temporary directories printed as `BROWSER_REPORT`. Reports include the engine;
baseline refresh is not automatic. A WebKit pass is targeted compatibility
evidence, not a claim that every Safari feature or page has been tested.

## Release compatibility

Ship the input-scoped stop observation change with its matching backend. Do not
publish only this frontend against an older backend, or roll back only the
backend: older chat schemas ignore `observe_client_message_id` and cannot enforce
the target-input boundary. Old frontends remain accepted by the new backend.
The release/protocol notification is advisory, not a deployment gate or capability
handshake. It never refreshes the page, cancels work, or resubmits analysis.
