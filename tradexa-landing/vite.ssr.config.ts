import { defineConfig } from "vite";
import react from "@vitejs/plugin-react";
import path from "node:path";

// The client build deliberately splits React and motion into long-lived chunks.
// SSR externalizes those packages, so it needs a separate output policy rather
// than trying to put external modules into the client's manual chunks.
export default defineConfig({
  plugins: [react()],
  resolve: {
    alias: { "@": path.resolve(__dirname, "./src") },
  },
});
