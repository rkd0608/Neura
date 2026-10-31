import { render, screen } from "@testing-library/react";
import { describe, expect, it } from "vitest";

import { Field } from "./Field";

describe("Field", () => {
  it("labels its control and describes it with its hint", () => {
    render(
      <Field id="name" label="Name" hint="What uses it">
        <input />
      </Field>,
    );
    const input = screen.getByLabelText("Name");
    expect(input).toHaveAccessibleDescription("What uses it");
    expect(input).not.toHaveAttribute("aria-invalid");
  });

  it("marks its control invalid and describes it with the error too", () => {
    render(
      <Field id="name" label="Name" hint="What uses it" error="Too long">
        <input />
      </Field>,
    );
    const input = screen.getByLabelText("Name");
    expect(input).toBeInvalid();
    expect(input).toHaveAccessibleDescription("What uses it Too long");
  });

  it("names a control with no hint or error by its label alone", () => {
    render(
      <Field id="site" label="Site">
        <select>
          <option>Lab</option>
        </select>
      </Field>,
    );
    expect(screen.getByRole("combobox", { name: "Site" })).not.toHaveAttribute("aria-describedby");
  });
});
