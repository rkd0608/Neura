import { act, render, screen } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import { ApiError } from "../api/client";
import { DataAge, ErrorState, Reason, Status } from "./ui";

describe("Status", () => {
  it("always shows a text label, not only a color", () => {
    render(<Status phase="NeedsAttention" />);
    expect(screen.getByText("Needs attention")).toHaveClass("badge", "danger");
  });

  it("greys out stale state", () => {
    render(<Status phase="Running" stale />);
    expect(screen.getByText("Running")).toHaveClass("muted");
  });
});

describe("Reason", () => {
  it("explains a reason code in plain language from the catalog", () => {
    render(<Reason code="AGENT_ACTIVATION_REFUSED" />);
    expect(screen.getByText("The site operator refused the activation.")).toBeInTheDocument();
    expect(screen.getByText("AGENT_ACTIVATION_REFUSED")).toBeInTheDocument();
  });
});

describe("DataAge", () => {
  beforeEach(() => vi.useFakeTimers());
  afterEach(() => vi.useRealTimers());

  it("ages from the server's data age plus local elapsed time, and says when it is stale", () => {
    const fetchedAt = Date.now();
    render(<DataAge ageSeconds={50} fetchedAt={fetchedAt} staleAfter={60} />);
    expect(screen.getByText("Updated 50s ago")).toBeInTheDocument();
    act(() => {
      vi.advanceTimersByTime(20_000);
    });
    expect(screen.getByText("Last seen 1m ago")).toBeInTheDocument();
  });

  it("says when nothing was ever reported", () => {
    render(<DataAge ageSeconds={null} fetchedAt={0} never="Never connected" />);
    expect(screen.getByText("Never connected")).toBeInTheDocument();
  });
});

describe("ErrorState", () => {
  it("shows the catalog explanation and the reason code of an API problem", () => {
    render(
      <ErrorState error={new ApiError(409, { reason: "API_CONFLICT", detail: "name taken" })} />,
    );
    expect(screen.getByRole("alert")).toHaveTextContent("name taken");
    expect(screen.getByRole("alert")).toHaveTextContent("API_CONFLICT");
  });
});
