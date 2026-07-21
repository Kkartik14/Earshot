import { Route, Routes } from "react-router-dom";
import styles from "./App.module.css";
import { FleetDashboard } from "./features/fleet/FleetDashboard";
import { SessionInspector } from "./features/inspector/SessionInspector";
import { SessionRail } from "./features/sessions/SessionRail";

export function App() {
  return (
    <div className={styles.shell}>
      <SessionRail />
      <main className={styles.main}>
        <Routes>
          <Route index element={<FleetDashboard />} />
          <Route path="sessions/:bundleId" element={<SessionInspector />} />
        </Routes>
      </main>
    </div>
  );
}
