import { memo, useEffect, useRef } from "react";
import {
  AreaSeries,
  createChart,
  type IChartApi,
  type ISeriesApi,
  type AreaData,
  type UTCTimestamp,
} from "lightweight-charts";

interface PriceChartProps {
  spark: number[] | null;
  height?: number;
}

function PriceChartImpl({ spark, height = 280 }: PriceChartProps) {
  const containerRef = useRef<HTMLDivElement | null>(null);
  const chartRef = useRef<IChartApi | null>(null);
  const seriesRef = useRef<ISeriesApi<"Area"> | null>(null);

  // Create the chart once.
  useEffect(() => {
    const el = containerRef.current;
    if (!el) return;

    const chart = createChart(el, {
      height,
      layout: {
        background: { color: "transparent" },
        textColor: "#62677a",
        attributionLogo: false,
      },
      grid: {
        vertLines: { color: "#232330" },
        horzLines: { color: "#232330" },
      },
      rightPriceScale: { borderColor: "#232330" },
      timeScale: {
        borderColor: "#232330",
        timeVisible: false,
        visible: false,
      },
      crosshair: { horzLine: { visible: false }, vertLine: { visible: false } },
      handleScroll: false,
      handleScale: false,
    });
    const series = chart.addSeries(AreaSeries, {
      lineColor: "#2dd4bf",
      topColor: "rgba(45, 212, 191, 0.25)",
      bottomColor: "rgba(45, 212, 191, 0.0)",
      lineWidth: 2,
      priceLineVisible: false,
      lastValueVisible: false,
    });
    chartRef.current = chart;
    seriesRef.current = series;

    const ro = new ResizeObserver((entries) => {
      const w = entries[0]?.contentRect.width;
      if (w) chart.applyOptions({ width: Math.floor(w) });
    });
    ro.observe(el);

    return () => {
      ro.disconnect();
      chart.remove();
      chartRef.current = null;
      seriesRef.current = null;
    };
  }, [height]);

  // Push data whenever the series changes. Fake hourly timestamps now-24h..now.
  useEffect(() => {
    const series = seriesRef.current;
    const chart = chartRef.current;
    if (!series || !chart) return;
    const points = spark ?? [];
    const nowSec = Math.floor(Date.now() / 1000);
    const step = 3600;
    const start = nowSec - step * (points.length - 1);
    const data: AreaData<UTCTimestamp>[] = points.map((v, i) => ({
      time: (start + i * step) as UTCTimestamp,
      value: v,
    }));
    series.setData(data);
    chart.timeScale().fitContent();
  }, [spark]);

  return <div ref={containerRef} className="w-full" style={{ height }} />;
}

export const PriceChart = memo(PriceChartImpl);
