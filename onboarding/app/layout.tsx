import type { Metadata } from "next";
import { Inter } from "next/font/google";
import "./globals.css";

const inter = Inter({
  variable: "--font-inter",
  subsets: ["latin"],
});

export const metadata: Metadata = {
  metadataBase: new URL("https://network-observatory-connect.openai.site"),
  title: "Connect Gmail | Network Observatory",
  description:
    "Create a private Gmail metadata connection for your Network Observatory and Hermes agent.",
  icons: {
    icon: "/favicon.svg",
    shortcut: "/favicon.svg",
  },
  openGraph: {
    title: "Connect Gmail | Network Observatory",
    description:
      "Optional, private relationship recency for your LinkedIn network map.",
    type: "website",
    images: [
      {
        url: "/og.png",
        width: 1200,
        height: 630,
        alt: "Network Observatory constellation",
      },
    ],
  },
  twitter: {
    card: "summary_large_image",
    title: "Connect Gmail | Network Observatory",
    description:
      "Optional, private relationship recency for your LinkedIn network map.",
    images: ["/og.png"],
  },
};

export default function RootLayout({
  children,
}: Readonly<{
  children: React.ReactNode;
}>) {
  return (
    <html lang="en">
      <body className={`${inter.variable} antialiased`}>{children}</body>
    </html>
  );
}
