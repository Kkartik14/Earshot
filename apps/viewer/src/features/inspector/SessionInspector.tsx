import { useParams } from "react-router-dom";

// Placeholder: the trace tree, drawer, and call-graph land in the next commits.
export function SessionInspector() {
  const { bundleId } = useParams<{ bundleId: string }>();
  return (
    <div style={{ padding: 20 }}>
      <h1 className="mono" style={{ fontSize: 15, margin: 0 }}>
        {bundleId}
      </h1>
    </div>
  );
}
