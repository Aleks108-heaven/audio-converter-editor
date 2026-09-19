export interface DownloadsNamespace {
  save(request: {
    filename: string;
    data: string | Blob | ArrayBuffer | ArrayBufferView;
    request?: string;
  }): Promise<{ status: "saved" | "delivered" }>;
}

declare global {
  interface Window {
    claude?: {
      use?: (name: string) => Promise<unknown>;
    };
  }
}

/** Resolves the `downloads` runtime capability, or null when this view can't
 * offer file saves (design for absence rather than failing loudly). */
export async function getDownloads(): Promise<DownloadsNamespace | null> {
  try {
    if (!window.claude?.use) return null;
    const ns = await window.claude.use("downloads");
    return (ns as DownloadsNamespace) ?? null;
  } catch {
    return null;
  }
}
