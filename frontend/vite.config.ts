/// <reference types="vitest/config" />
import { defineConfig } from "vite";
import react from "@vitejs/plugin-react";
import path from "node:path";

// Cổng backend đổi được qua biến môi trường: máy nhiều người đã có thứ khác
// chiếm cổng 8000, và sửa file cấu hình chung để chạy được máy mình là cách
// nhanh nhất tạo ra một commit làm hỏng máy người khác.
const API_TARGET = process.env.VITE_API_PROXY_TARGET ?? "http://localhost:8000";

export default defineConfig({
  plugins: [react()],
  resolve: { alias: { "@": path.resolve(__dirname, "./src") } },
  server: {
    port: 5173,
    host: true,
    proxy: {
      // Gọi /api/... từ frontend sẽ được chuyển tới backend, tránh vấn đề CORS khi dev
      "/api": { target: API_TARGET, changeOrigin: true, ws: true },
    },
  },
  test: {
    environment: "jsdom",
    globals: true,
    setupFiles: ["./src/test/setup.ts"],
  },
});
