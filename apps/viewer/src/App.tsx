import { Route, Routes } from "react-router-dom";
import styles from "./App.module.css";
import { EmptyState } from "./components/EmptyState";
import { SessionInspector } from "./features/inspector/SessionInspector";
import { SessionRail } from "./features/sessions/SessionRail";

export function App() {
  return (
    <div className={styles.shell}>
      <SessionRail />
      <main className={styles.main}>
        <Routes>
          <Route
            index
            element={
              <EmptyState
                title="Select a session"
                hint="Pick a voice session on the left to inspect its turns, latency, and call-graph."
              />
            }
          />
          <Route path="sessions/:bundleId" element={<SessionInspector />} />
        </Routes>
      </main>
    </div>
  );
}
