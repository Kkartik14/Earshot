import type { Metadata } from "next";
import "@earshot/viewer-ui/styles/tokens.css";
import "@earshot/viewer-ui/styles/global.css";
import { Providers } from "./providers";
import { StandaloneAuth } from "./StandaloneAuth";

export const metadata: Metadata = {
  title: "Earshot Observe",
  description: "Evidence-based voice AI observability",
};

export default function RootLayout({
  children,
}: Readonly<{ children: React.ReactNode }>) {
  return (
    <html lang="en">
      <body>
        <Providers>
          <StandaloneAuth>{children}</StandaloneAuth>
        </Providers>
      </body>
    </html>
  );
}
