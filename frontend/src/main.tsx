import { StrictMode } from "react";
import { createRoot } from "react-dom/client";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { BrowserRouter, Routes, Route, Link } from "react-router-dom";
import Dashboard from "./pages/Dashboard";
import Subnet from "./pages/Subnet";
import "./index.css";

const queryClient = new QueryClient({
  defaultOptions: {
    queries: {
      staleTime: 60_000,
      refetchInterval: 120_000,
      retry: 1,
    },
  },
});

function NotFound() {
  return (
    <main className="max-w-[1200px] mx-auto px-5 pt-24 flex justify-center">
      <div className="text-center">
        <div className="text-ink font-medium text-lg">Page not found</div>
        <Link
          to="/"
          className="inline-block mt-3 text-accent hover:underline text-[14px]"
        >
          ← Back to dashboard
        </Link>
      </div>
    </main>
  );
}

const root = document.getElementById("root");
if (!root) throw new Error("#root element missing");

createRoot(root).render(
  <StrictMode>
    <QueryClientProvider client={queryClient}>
      <BrowserRouter>
        <Routes>
          <Route path="/" element={<Dashboard />} />
          <Route path="/subnet/:netuid" element={<Subnet />} />
          <Route path="*" element={<NotFound />} />
        </Routes>
      </BrowserRouter>
    </QueryClientProvider>
  </StrictMode>,
);
