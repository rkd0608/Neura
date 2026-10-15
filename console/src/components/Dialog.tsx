// A modal built on the native <dialog>: focus is trapped and Escape closes it; focus returns to
// whatever opened it.
import { useEffect, useId, useRef, type ReactNode } from "react";

export function Dialog({
  open,
  title,
  description,
  onClose,
  children,
}: {
  open: boolean;
  title: string;
  description?: ReactNode;
  onClose: () => void;
  children: ReactNode;
}) {
  const ref = useRef<HTMLDialogElement>(null);
  const titleId = useId();
  const descriptionId = useId();

  useEffect(() => {
    const dialog = ref.current;
    if (!dialog) return;
    if (open && !dialog.open) {
      const opener = document.activeElement instanceof HTMLElement ? document.activeElement : null;
      dialog.showModal();
      const restore = () => opener?.focus();
      dialog.addEventListener("close", restore, { once: true });
    } else if (!open && dialog.open) {
      dialog.close();
    }
  }, [open]);

  return (
    <dialog
      ref={ref}
      aria-labelledby={titleId}
      aria-describedby={description ? descriptionId : undefined}
      onClose={onClose}
      onCancel={(event) => {
        event.preventDefault();
        onClose();
      }}
    >
      {open ? (
        <>
          <div className="dialog-header">
            <h2 id={titleId}>{title}</h2>
            {description ? (
              <p id={descriptionId} className="secondary" style={{ marginTop: 4 }}>
                {description}
              </p>
            ) : null}
          </div>
          {children}
        </>
      ) : null}
    </dialog>
  );
}
