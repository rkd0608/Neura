// The entry point: choose an organization and project, or create the first project.
import { useState } from "react";
import { Link, Navigate, useNavigate, useParams } from "react-router";

import { api, unwrap, idempotent } from "../api/client";
import { useApiMutation, useMe, useProjects } from "../api/queries";
import { FolderIcon } from "../components/Icons";
import { Field } from "../components/Field";
import { Card, ErrorState, Loading, PageHeader } from "../components/ui";
import { usePermissions } from "../lib/context";
import { slug } from "../lib/format";

export function Start() {
  const me = useMe();
  if (me.isPending) return <Loading />;
  if (me.isError) return <ErrorState error={me.error} />;
  const org = me.data.organizations[0];
  if (!org) {
    return (
      <div className="center-page">
        <Card title="No organization yet">
          <p>
            Your account is not a member of any organization. Ask an admin to invite you, then sign
            in again.
          </p>
        </Card>
      </div>
    );
  }
  return <ChooseProject orgId={org.id} />;
}

function ChooseProject({ orgId }: { orgId: string }) {
  const projects = useProjects(orgId);
  if (projects.isPending) return <Loading />;
  if (projects.isError) return <ErrorState error={projects.error} />;
  const remembered = localStorageGet(`neura.project.${orgId}`);
  const target =
    projects.data.find((p) => p.metadata.id === remembered) ?? projects.data[0] ?? null;
  if (target) return <Navigate to={`/o/${orgId}/p/${target.metadata.id}`} replace />;
  return <Navigate to={`/o/${orgId}/projects?first=1`} replace />;
}

export function localStorageGet(key: string): string | null {
  try {
    return localStorage.getItem(key);
  } catch {
    return null;
  }
}

export function localStorageSet(key: string, value: string): void {
  try {
    localStorage.setItem(key, value);
  } catch {
    // Remembering the last project is a convenience only.
  }
}

export function NewProjectForm({
  orgId,
  onCreated,
}: {
  orgId: string;
  onCreated?: (id: string) => void;
}) {
  const navigate = useNavigate();
  const [name, setName] = useState("");
  const create = useApiMutation(
    async (displayName: string) =>
      unwrap(
        await api.POST("/v1/organizations/{organization_id}/projects", {
          params: { path: { organization_id: orgId }, header: idempotent() },
          body: { spec: { name: slug(displayName) || "project", display_name: displayName } },
        }),
      ),
    [["org", orgId, "projects"]],
  );
  return (
    <form
      className="stack"
      onSubmit={(event) => {
        event.preventDefault();
        create.mutate(name.trim(), {
          onSuccess: (project) => {
            onCreated?.(project.metadata.id);
            void navigate(`/o/${orgId}/p/${project.metadata.id}`);
          },
        });
      }}
    >
      <Field
        id="project-name"
        label="Project name"
        hint={
          name
            ? `Identifier: ${slug(name) || "project"}`
            : "For example, the robot program or customer line."
        }
      >
        <input
          type="text"
          required
          maxLength={200}
          value={name}
          onChange={(event) => setName(event.target.value)}
        />
      </Field>
      {create.isError ? (
        <ErrorState error={create.error} title="Could not create the project" />
      ) : null}
      <div className="form-actions">
        <button
          type="submit"
          className="button primary"
          disabled={!name.trim() || create.isPending}
        >
          {create.isPending ? "Creating…" : "Create project"}
        </button>
      </div>
    </form>
  );
}

export function Projects() {
  const { orgId = "" } = useParams();
  const projects = useProjects(orgId);
  const can = usePermissions(orgId);
  const first = new URLSearchParams(window.location.search).has("first");
  return (
    <>
      <PageHeader
        icon={<FolderIcon />}
        title={first ? "Welcome to Neura" : "Projects"}
        description={
          first
            ? "Start with a project: it groups the sites, robots, models and deployments of one robot program."
            : "Each project groups the sites, robots, models and deployments of one robot program."
        }
      />
      <div className="grid cols-3">
        {!first
          ? projects.isPending
            ? [<Loading key="loading" />]
            : projects.isError
              ? [<ErrorState key="error" error={projects.error} />]
              : projects.data.map((p) => (
                  <Link
                    key={p.metadata.id}
                    to={`/o/${orgId}/p/${p.metadata.id}`}
                    className="card interactive"
                    style={{ color: "inherit", textDecoration: "none" }}
                  >
                    <div className="card-body stack" style={{ gap: 10 }}>
                      <span className="icon-tile">
                        <FolderIcon />
                      </span>
                      <div>
                        <div style={{ fontWeight: 600 }}>{p.spec.display_name}</div>
                        <div className="muted small mono">{p.spec.name}</div>
                      </div>
                      <span className="small" style={{ color: "var(--accent)" }}>
                        Open project →
                      </span>
                    </div>
                  </Link>
                ))
          : null}
        <Card title="Create a project">
          {can.admin ? (
            <NewProjectForm orgId={orgId} />
          ) : (
            <p className="secondary">An admin of this organization creates projects.</p>
          )}
        </Card>
      </div>
    </>
  );
}
