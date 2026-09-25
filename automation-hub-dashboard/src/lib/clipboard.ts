type Toast = (message: string, tone?: "success" | "error" | "info") => void;

/** Copy text and say what happened. Browsers refuse clipboard writes outside
 *  a secure context or without permission; that has to reach the user as a
 *  message, not escape as an unhandled promise rejection. */
export async function copyText(text: string, toast: Toast, what = "Link"): Promise<boolean> {
  try {
    if (!navigator.clipboard) throw new Error("clipboard unavailable");
    await navigator.clipboard.writeText(text);
    toast(`${what} copied`, "success");
    return true;
  } catch {
    toast("Could not copy: the browser blocked clipboard access", "error");
    return false;
  }
}
