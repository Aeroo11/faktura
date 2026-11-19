import type { Metadata, Viewport } from "next";
import { Geist, Geist_Mono } from "next/font/google";
import Link from "next/link";
import { FileCheck2 } from "lucide-react";
import { UserSwitcher } from "@/components/user-switcher";
import { currentUser } from "@/lib/api";
import "./globals.css";

const geistSans = Geist({ variable: "--font-geist-sans", subsets: ["latin"], display: "swap" });
const geistMono = Geist_Mono({ variable: "--font-geist-mono", subsets: ["latin"], display: "swap" });

export const metadata: Metadata = {
  title: { default: "Faktura — AP review queue", template: "%s · Faktura" },
  description: "Invoice exceptions, maker-checker approvals, and a tamper-evident audit trail.",
};

export const viewport: Viewport = { themeColor: "#f4f6fa", colorScheme: "light" };

export default async function RootLayout({ children }: Readonly<{ children: React.ReactNode }>) {
  const user = await currentUser();
  return (
    <html lang="en" className={`${geistSans.variable} ${geistMono.variable}`}>
      <body>
        <header className="border-b border-border bg-surface">
          <div className="mx-auto flex max-w-7xl flex-wrap items-center justify-between gap-3 px-4 py-3 sm:px-6">
            <Link href="/" className="inline-flex items-center gap-2 font-semibold tracking-tight">
              <FileCheck2 size={20} strokeWidth={1.75} className="text-accent" aria-hidden />
              Faktura
              <span className="hidden text-sm font-normal text-text-muted sm:inline">AP review queue</span>
            </Link>
            <UserSwitcher current={user.name} />
          </div>
        </header>
        <main className="mx-auto max-w-7xl px-4 py-6 sm:px-6">{children}</main>
        <footer className="mx-auto max-w-7xl px-4 pb-8 text-xs text-text-subtle sm:px-6">
          Synthetic demo data — every document is marked “CONTOH SINTETIS”. Identity is a dev-only
          header until SSO is added.
        </footer>
      </body>
    </html>
  );
}
