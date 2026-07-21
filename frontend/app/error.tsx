"use client";

import { useEffect } from "react";

export default function GlobalError({
  error,
  reset,
}: {
  error: Error & { digest?: string };
  reset: () => void;
}) {
  useEffect(() => {
    console.error(error);
  }, [error]);

  return (
    <main className="wrap">
      <div className="card">
        <div className="card-title" style={{ marginBottom: 8 }}>
          Something went wrong
        </div>
        <div className="alert alert-error" style={{ marginBottom: 16 }}>
          {error.message || "An unexpected error occurred."}
        </div>
        <button onClick={reset}>Try again</button>
      </div>
    </main>
  );
}
