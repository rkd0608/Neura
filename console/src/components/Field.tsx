// A form field: a label, its one control, a hint and an error, wired to each other in one place,
// so a control is always named by its label and described by its hint and error.
import { cloneElement, type ReactElement, type ReactNode } from "react";

/** What Field sets on its control. */
interface ControlProps {
  id?: string;
  "aria-describedby"?: string;
  "aria-invalid"?: boolean;
}

export function Field({
  label: text,
  hint,
  error,
  children,
  id,
}: {
  label: string;
  hint?: ReactNode;
  error?: string | null;
  id: string;
  /** The control (an input, select or textarea): Field gives it its id and descriptions. */
  children: ReactElement<ControlProps>;
}) {
  const hintId = `${id}-hint`;
  const errorId = `${id}-error`;
  const describedBy = [hint ? hintId : null, error ? errorId : null].filter(Boolean).join(" ");
  return (
    <div className="field">
      <label htmlFor={id}>{text}</label>
      {cloneElement(children, {
        id,
        "aria-describedby": describedBy || undefined,
        "aria-invalid": error ? true : undefined,
      })}
      {hint ? (
        <span className="hint" id={hintId}>
          {hint}
        </span>
      ) : null}
      {error ? (
        <span className="error" id={errorId} role="alert">
          {error}
        </span>
      ) : null}
    </div>
  );
}
