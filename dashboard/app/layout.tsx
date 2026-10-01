import type { Metadata, Viewport } from "next";
import "./globals.css";

export const viewport: Viewport = {
  width: "device-width",
  initialScale: 1,
  viewportFit: "cover",
  themeColor: "#1d211c",
};

export async function generateMetadata(): Promise<Metadata> {
  return {
    title: "LifeOS Control Center",
    description: "健康、訓練、資產與自動化管線的私人監控中心。",
    icons: { icon: "/favicon.svg", shortcut: "/favicon.svg" },
    openGraph: {
      title: "LifeOS Control Center",
      description: "Health · Training · Wealth · Systems",
    },
    twitter: { card: "summary" },
  };
}

export default function RootLayout({ children }: Readonly<{ children: React.ReactNode }>) {
  return <html lang="zh-Hant"><body>{children}</body></html>;
}
