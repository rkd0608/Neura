import { render, screen } from "@testing-library/react";
import type { ReactNode } from "react";
import { describe, expect, it } from "vitest";

import type { Schemas } from "../api/client";
import { EvaluationRow, TrainingRow } from "./Improve";

const metadata = {
  id: "evl_01JA2B3C4D5E6F7G8H9JKMNPQR",
  version: "3",
  created_at: "2026-10-18T09:00:00Z",
  updated_at: "2026-10-18T09:05:00Z",
  project_id: "prj_01JA2B3C4D5E6F7G8H9JKMNPQR",
};

function table(row: ReactNode) {
  return render(
    <table>
      <tbody>{row}</tbody>
    </table>,
  );
}

function evaluation(status: Partial<Schemas["Evaluation"]["status"]>): Schemas["Evaluation"] {
  return {
    metadata,
    deployment_id: "dep_01JA2B3C4D5E6F7G8H9JKMNPQR",
    spec: {
      candidate_revision_id: "rev_01JA2B3C4D5E6F7G8H9JKMNPQ1",
      baseline_revision_id: "rev_01JA2B3C4D5E6F7G8H9JKMNPQ0",
      suite: {
        kind: "replay",
        stride: 5,
        max_episodes: 200,
        relative: 0.25,
        absolute: 0.01,
        batch_size: 8,
      },
    },
    status: { phase: "Succeeded", ...status },
  };
}

describe("EvaluationRow", () => {
  it("states the verdict in words and the replay errors behind it", () => {
    table(
      <EvaluationRow
        fineTuned
        evaluation={evaluation({
          verdict: "non_inferior",
          report: {
            episodes: 12,
            candidate: { success: { samples: 90, mean: 0.021 } },
            baseline: { success: { samples: 90, mean: 0.018 } },
          },
        })}
      />,
    );
    expect(screen.getByText("No worse")).toHaveClass("badge", "ok");
    expect(screen.getByText(/2\.1% vs 1\.8%/)).toBeInTheDocument();
    expect(screen.getByText(/12 episodes/)).toBeInTheDocument();
    expect(screen.getByText("Fine-tuned")).toBeInTheDocument();
  });

  it("shows a refused report by its reason, without a verdict", () => {
    table(
      <EvaluationRow
        fineTuned={false}
        evaluation={evaluation({
          phase: "Failed",
          reason: "EVALUATION_REPORT_INVALID",
          message: "the site reported no replay evaluation",
        })}
      />,
    );
    expect(screen.getByText("Failed")).toBeInTheDocument();
    expect(screen.getByText("EVALUATION_REPORT_INVALID")).toBeInTheDocument();
    expect(screen.queryByText("No worse")).not.toBeInTheDocument();
  });
});

describe("TrainingRow", () => {
  it("shows what the model was trained on, its loss and the candidate it became", () => {
    table(
      <TrainingRow
        job={{
          metadata: { ...metadata, id: "trn_01JA2B3C4D5E6F7G8H9JKMNPQR" },
          deployment_id: "dep_01JA2B3C4D5E6F7G8H9JKMNPQR",
          spec: {
            base_revision_id: "rev_01JA2B3C4D5E6F7G8H9JKMNPQ0",
            artifact_name: "act-so101",
            artifact_version: "1.0.0-ft.abcdefgh",
          },
          status: {
            phase: "Succeeded",
            dataset_episodes: 3,
            dataset_digest: `sha256:${"d".repeat(64)}`,
            metrics: { steps: 2000, loss_first: 1.2, loss_last: 0.4 },
            candidate_revision_id: "rev_01JA2B3C4D5E6F7G8H9JKMNPQ1",
          },
        }}
      />,
    );
    expect(screen.getByText("act-so101 1.0.0-ft.abcdefgh")).toBeInTheDocument();
    expect(screen.getByText(/3 episodes/)).toBeInTheDocument();
    expect(screen.getByText(/1\.200 → 0\.400/)).toBeInTheDocument();
    expect(screen.getByText("Succeeded")).toBeInTheDocument();
  });
});
