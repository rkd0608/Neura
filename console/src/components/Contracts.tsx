// Registering and showing contract documents (design §4): model artifacts, robot profiles and
// execution profiles. Documents are immutable once registered; a change is a new document.
import { useState } from "react";
import { parse } from "yaml";

import { api, unwrap, type Schemas, idempotent } from "../api/client";
import { useApiMutation } from "../api/queries";
import { shortDigest } from "../lib/format";
import { Dialog } from "./Dialog";
import { useToasts } from "./Toasts";
import { ErrorState, Field } from "./ui";

export type Kind = "ModelArtifact" | "RobotProfile" | "ExecutionProfile";
export type Registered =
  Schemas["ModelArtifact"] | Schemas["RobotProfile"] | Schemas["ExecutionProfile"];

const PATHS = {
  ModelArtifact: "/v1/projects/{project_id}/model-artifacts",
  RobotProfile: "/v1/projects/{project_id}/robot-profiles",
  ExecutionProfile: "/v1/projects/{project_id}/execution-profiles",
} as const;

const COLLECTIONS: Record<Kind, string> = {
  ModelArtifact: "model-artifacts",
  RobotProfile: "robot-profiles",
  ExecutionProfile: "execution-profiles",
};

export const KIND_LABELS: Record<Kind, string> = {
  ModelArtifact: "model artifact",
  RobotProfile: "robot profile",
  ExecutionProfile: "execution profile",
};

interface Meta {
  name?: string;
  version?: string;
  description?: string;
}

/** "a robot profile", "an execution profile". */
export function withArticle(text: string): string {
  return `${/^[aeiou]/i.test(text) ? "an" : "a"} ${text}`;
}

export function documentMeta(doc: Registered | undefined): Meta {
  const metadata: Meta = doc?.document.metadata ?? {};
  return metadata;
}

export function documentTitle(doc: Registered | undefined): string {
  const meta = documentMeta(doc);
  if (!meta.name) return doc ? shortDigest(doc.digest) : "—";
  return meta.version ? `${meta.name} ${meta.version}` : meta.name;
}

/** Parse a YAML or JSON contract document, checking only what the console needs to route it;
 *  the server validates the document fully. */
export function parseDocument(text: string, kind: Kind): Schemas["ContractDocument"] {
  let value: unknown;
  try {
    value = parse(text);
  } catch (error) {
    throw new Error(
      `Not valid YAML or JSON: ${error instanceof Error ? error.message : String(error)}`,
      {
        cause: error,
      },
    );
  }
  if (typeof value !== "object" || value === null || Array.isArray(value)) {
    throw new Error("The document must be a YAML or JSON object.");
  }
  const doc = value as Record<string, unknown>;
  if (doc.kind !== kind) {
    const found =
      typeof doc.kind === "string"
        ? withArticle(doc.kind in KIND_LABELS ? KIND_LABELS[doc.kind as Kind] : doc.kind)
        : "a document without a kind";
    throw new Error(`This is ${found}, not ${withArticle(KIND_LABELS[kind])}.`);
  }
  return doc as Schemas["ContractDocument"];
}

export function RegisterContract({
  kind,
  open,
  onClose,
  projectId,
}: {
  kind: Kind;
  open: boolean;
  onClose: () => void;
  projectId: string;
}) {
  const toasts = useToasts();
  const [text, setText] = useState("");
  const [parseError, setParseError] = useState<string | null>(null);
  const register = useApiMutation(
    async (document: Schemas["ContractDocument"]) => {
      const result = await api.POST(PATHS[kind], {
        params: { path: { project_id: projectId }, header: idempotent() },
        body: { document },
      });
      const registered: Registered = unwrap(result);
      return registered;
    },
    [["p", projectId, COLLECTIONS[kind]]],
  );
  const label = KIND_LABELS[kind];

  return (
    <Dialog
      open={open}
      onClose={onClose}
      title={`Register ${withArticle(label)}`}
      description="Upload or paste the YAML or JSON document. It is validated and stored with its content digest; registering the same document again returns the existing one."
    >
      <form
        onSubmit={(event) => {
          event.preventDefault();
          let document: Schemas["ContractDocument"];
          try {
            document = parseDocument(text, kind);
          } catch (error) {
            setParseError(error instanceof Error ? error.message : String(error));
            return;
          }
          register.mutate(document, {
            onSuccess: (registered) => {
              toasts.ok(
                `Registered ${documentTitle(registered)} (${shortDigest(registered.digest)}).`,
              );
              setText("");
              onClose();
            },
          });
        }}
      >
        <div className="dialog-body">
          <Field id={`${kind}-file`} label="File">
            <input
              id={`${kind}-file`}
              type="file"
              accept=".yaml,.yml,.json,application/json,application/yaml"
              onChange={(event) => {
                const file = event.target.files?.[0];
                if (!file) return;
                void file.text().then((content) => {
                  setText(content);
                  setParseError(null);
                });
              }}
            />
          </Field>
          <Field id={`${kind}-text`} label="Document" error={parseError}>
            <textarea
              id={`${kind}-text`}
              value={text}
              spellCheck={false}
              aria-invalid={parseError ? true : undefined}
              onChange={(event) => {
                setText(event.target.value);
                setParseError(null);
              }}
            />
          </Field>
          {register.isError ? (
            <ErrorState error={register.error} title={`The ${label} was not accepted`} />
          ) : null}
        </div>
        <div className="dialog-footer form-actions">
          <button type="button" className="button" onClick={onClose}>
            Cancel
          </button>
          <button
            type="submit"
            className="button primary"
            disabled={!text.trim() || register.isPending}
          >
            {register.isPending ? "Registering…" : "Register"}
          </button>
        </div>
      </form>
    </Dialog>
  );
}

/** How a revision's batch group shares its device (design §9.5), as capacity estimates say. */
export const SHARING_CASES: Record<string, { label: string; detail: string }> = {
  same_weights: {
    label: "Batched",
    detail: "Robots on this revision share forward passes, and nothing else runs on the device.",
  },
  multi_adapter: {
    label: "Multi-adapter",
    detail: "One base-model forward pass serves several adapters.",
  },
  co_located_fine_tunes: {
    label: "Co-located fine-tunes",
    detail:
      "Other fine-tunes of the same architecture run their own workers on this device. The estimate assumes no contention from them; see the benchmark reports.",
  },
  different_architectures: {
    label: "Shares the device",
    detail:
      "Other models run their own workers on this device. The estimate assumes no contention from them; see the benchmark reports.",
  },
};

export function sharingCase(value: string | null | undefined): { label: string; detail: string } {
  return SHARING_CASES[value ?? "same_weights"] ?? { label: value ?? "", detail: "" };
}
