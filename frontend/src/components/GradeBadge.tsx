import { memo } from "react";

type Grade = "A" | "B" | "C" | "D" | "F";

interface GradeBadgeProps {
  grade: Grade;
  provisional?: boolean;
}

const gradeVar: Record<Grade, string> = {
  A: "--color-grade-a",
  B: "--color-grade-b",
  C: "--color-grade-c",
  D: "--color-grade-d",
  F: "--color-grade-f",
};

function GradeBadgeImpl({ grade, provisional }: GradeBadgeProps) {
  const v = `var(${gradeVar[grade]})`;
  return (
    <span
      className="inline-flex items-center justify-center w-[22px] h-[22px] rounded-[4px] font-semibold text-[12px]"
      style={{
        color: v,
        backgroundColor: `color-mix(in srgb, ${v} 14%, transparent)`,
      }}
    >
      {grade}
      {provisional ? (
        <sup
          className="text-[8px] ml-px"
          title="Provisional score"
        >
          *
        </sup>
      ) : null}
    </span>
  );
}

export const GradeBadge = memo(GradeBadgeImpl);
