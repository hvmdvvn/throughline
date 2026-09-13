"use client";

import { cn } from "@/lib/utils";

type Point = { label: string; value: number };

/**
 * Minimal SVG sparkline — no chart library. Values are drawn left→right.
 */
export function TrendSparkline({
  points,
  className,
  formatValue,
}: {
  points: Point[];
  className?: string;
  formatValue?: (value: number) => string;
}) {
  if (points.length < 2) {
    return null;
  }

  const width = 220;
  const height = 48;
  const padX = 4;
  const padY = 6;
  const values = points.map((p) => p.value);
  const min = Math.min(...values);
  const max = Math.max(...values);
  const span = max - min || 1;

  const coords = points.map((p, i) => {
    const x =
      padX + (i / (points.length - 1)) * (width - padX * 2);
    const y =
      height - padY - ((p.value - min) / span) * (height - padY * 2);
    return { x, y, ...p };
  });

  const path = coords
    .map((c, i) => `${i === 0 ? "M" : "L"} ${c.x.toFixed(1)} ${c.y.toFixed(1)}`)
    .join(" ");

  const latest = points[points.length - 1];
  const first = points[0];

  return (
    <div className={cn("flex flex-col gap-1", className)}>
      <svg
        viewBox={`0 0 ${width} ${height}`}
        className="h-12 w-full max-w-[14rem] text-foreground"
        role="img"
        aria-label={`Trend from ${first.label} to ${latest.label}`}
      >
        <path
          d={path}
          fill="none"
          stroke="currentColor"
          strokeWidth="1.75"
          strokeLinecap="round"
          strokeLinejoin="round"
          opacity={0.85}
        />
        {coords.map((c) => (
          <circle
            key={`${c.label}-${c.value}`}
            cx={c.x}
            cy={c.y}
            r={2.25}
            fill="currentColor"
          />
        ))}
      </svg>
      <p className="text-[11px] text-muted-foreground">
        {first.label}
        <span className="mx-1.5 text-border">→</span>
        {latest.label}
        {formatValue ? (
          <span className="ml-2 font-medium text-foreground">
            {formatValue(latest.value)}
          </span>
        ) : null}
      </p>
    </div>
  );
}
