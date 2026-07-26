// Live progress for a long-running tool call. A delegated coding task runs for minutes, so
// silence between tool_started and tool_finished reads as a hang. Turns start COLLAPSED, so
// the header's live line is the pulse the user actually watches; the expanded step row
// carries the full run. Progress is display-only and never persisted, which is why it
// disappears the moment the call completes.
import { expect } from "@playwright/test";
import { test } from "./fixtures";

test("a delegation streams progress onto the collapsed turn header while it runs", async ({
  page,
}) => {
  await page.goto("/");
  await page.getByPlaceholder(/Ask the coworker/).fill("delegate the parser work");
  await page.getByRole("button", { name: "Send" }).click();

  // EXEC-gated: nothing runs, and no progress is rendered anywhere, until the user approves.
  const allow = page.getByRole("button", { name: "Allow once" });
  await expect(allow).toBeVisible();
  await expect(page.getByText("Edit · src/cli.py")).toHaveCount(0);

  await allow.click();

  // Now it's running: the header tracks the delegate's newest line. Which line is showing
  // at any instant is timing-dependent (they arrive ~80ms apart) — the deterministic
  // ordering is pinned in Transcript.test.tsx; here we prove the wire → render path.
  const live = page.getByTestId("turn-live-line");
  await expect(live).toContainText("src/cli.py");
  await page.screenshot({ path: "test-results/tool-progress-collapsed.png" });

  // And it ends with the real result, not a progress line.
  await expect(page.getByText(/Claude Code added the flag and a test/)).toBeVisible();
});

test("the expanded step row lists the progress lines, then drops them when done", async ({
  page,
}) => {
  await page.goto("/");
  await page.getByPlaceholder(/Ask the coworker/).fill("delegate the parser work");
  await page.getByRole("button", { name: "Send" }).click();

  await page.getByRole("button", { name: "Allow once" }).click();

  // Expand the running turn: the growing list sits under the step row.
  await page.locator("summary.stepgroup-head").first().click();
  const progress = page.getByTestId("tool-progress");
  await expect(progress).toContainText("Read · src/cli.py");
  await page.screenshot({ path: "test-results/tool-progress-expanded.png" });

  // Once the call returns, the live lines go away — a reloaded transcript has none, and the
  // result preview is what remains (one click away under `raw`).
  await expect(page.getByText(/Claude Code added the flag and a test/)).toBeVisible();
  await expect(page.getByTestId("tool-progress")).toHaveCount(0);
});

test("denying a delegation streams no progress at all", async ({ page }) => {
  await page.goto("/");
  await page.getByPlaceholder(/Ask the coworker/).fill("delegate the parser work");
  await page.getByRole("button", { name: "Send" }).click();

  await page.getByRole("button", { name: "Deny" }).click();

  await expect(page.getByText(/left the code alone/)).toBeVisible();
  await expect(page.getByTestId("tool-progress")).toHaveCount(0);
});
