import { describe, expect, it } from "bun:test";
import { renderJson, renderPretty } from "./render.ts";
import { parsePrNumber, type WatcherVerdict } from "./types.ts";

const hostile = "remote\x1b[2J\r\x1b]52;c;dummy\x07\x9b1A\nforged";
const context = { owner: "owner", repo: "repo", number: parsePrNumber(1) };
const common = { schemaVersion: 1, sequence: 1, observedAt: "now", mode: "single", terminal: true } as const;
const fixtures: WatcherVerdict[] = [
  { ...common, exitCode: 3, kind: "BLOCKER", blocker: { kind: "review-threads", pr: context, threads: [{ id: hostile, isBugbot: false, bugbotReviewPasses: 0, firstComment: { authorLogin: hostile, path: hostile, body: hostile, line: 1, createdAt: "now" } }] } },
  { ...common, exitCode: 4, kind: "BLOCKER", blocker: { kind: "failing-checks", pr: context, ci: { kind: "ci-failing", failed: [{ kind: "failed", name: hostile, description: hostile, link: hostile, workflow: hostile, reportedState: hostile }], pending: [], hadPreviousPassingCi: false, source: "gh-pr-checks", all: [{ kind: "failed", name: hostile, description: hostile, link: hostile, workflow: hostile, reportedState: hostile }], github: { kind: "allowed", basis: "merge-state", mergeStateStatus: "CLEAN", headRollupState: "SUCCESS" } } } },
  { ...common, exitCode: 7, kind: "BLOCKER", blocker: { kind: "status-query", failures: 1, failure: { kind: "stack-graph", retryable: false, detail: hostile } } },
];

describe("remote terminal metadata", () => {
  for (const fixture of fixtures) {
    it(`escapes ${fixture.kind === "BLOCKER" ? fixture.blocker.kind : fixture.kind} controls while preserving layout`, () => {
      const pretty = renderPretty(fixture);
      expect(pretty).not.toMatch(/[\x00-\x09\x0b-\x1f\x7f-\x9f]/);
      expect(pretty).toContain("\\u001b");
      expect(pretty).toContain("\\u000d");
      expect(pretty).toContain("\\u0007");
      expect(pretty).toContain("\\u009b");
      expect(pretty).toContain("\n");
      expect(JSON.parse(renderJson(fixture))).toEqual(fixture);
    });
  }
});
