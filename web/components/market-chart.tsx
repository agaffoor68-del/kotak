"use client";

import { useEffect, useRef, useMemo } from "react";
import {
  createChart,
  ColorType,
  CrosshairMode,
  type IChartApi,
  CandlestickSeries,
  HistogramSeries,
  type CandlestickData,
  type HistogramData,
  type Time,
} from "lightweight-charts";
import type { Candle } from "@/lib/api";
import { Zap } from "lucide-react";

interface MarketChartProps {
  candles: Candle[];
  symbol: string;
  ltp: number;
  changePercent: number;
  onTrade?: () => void;
}

export function MarketChart({
  candles,
  symbol,
  ltp,
  changePercent,
  onTrade,
}: MarketChartProps) {
  const chartContainerRef = useRef<HTMLDivElement>(null);
  const chartRef = useRef<IChartApi | null>(null);

  // Sanitize candles: filter out any candles where open, high, low, or close is 0, null, or undefined
  const sanitizedCandles = useMemo(() => {
    return candles.filter(
      (c) =>
        c.open != null &&
        c.high != null &&
        c.low != null &&
        c.close != null &&
        c.open !== 0 &&
        c.high !== 0 &&
        c.low !== 0 &&
        c.close !== 0 &&
        !isNaN(c.open) &&
        !isNaN(c.high) &&
        !isNaN(c.low) &&
        !isNaN(c.close)
    );
  }, [candles]);

  useEffect(() => {
    if (!chartContainerRef.current) return;

    const chart = createChart(chartContainerRef.current, {
      layout: {
        background: { type: ColorType.Solid, color: "#131722" },
        textColor: "#d1d4dc",
        fontFamily: "Inter, -apple-system, BlinkMacSystemFont, sans-serif",
      },
      grid: {
        vertLines: { color: "#1e222d" },
        horzLines: { color: "#1e222d" },
      },
      crosshair: { mode: CrosshairMode.Normal },
      rightPriceScale: { borderColor: "#2a2e39", visible: true },
      timeScale: { borderColor: "#2a2e39", timeVisible: true, secondsVisible: false },
      width: chartContainerRef.current.clientWidth,
      height: chartContainerRef.current.clientHeight,
    });

    chartRef.current = chart;

    const cs = chart.addSeries(CandlestickSeries, {
      upColor: "#089981",
      downColor: "#f23645",
      borderVisible: false,
      wickUpColor: "#089981",
      wickDownColor: "#f23645",
    });

    const vs = chart.addSeries(HistogramSeries, {
      color: "#26a69a",
      priceFormat: { type: "volume" },
      priceScaleId: "",
    });
    vs.priceScale().applyOptions({ scaleMargins: { top: 0.82, bottom: 0 } });

    // Set data with sanitization
    if (sanitizedCandles.length > 0) {
      const cData: CandlestickData<Time>[] = sanitizedCandles.map((c) => ({
        time: c.time as Time,
        open: c.open,
        high: c.high,
        low: c.low,
        close: c.close,
      }));
      const vData: HistogramData<Time>[] = sanitizedCandles.map((c) => ({
        time: c.time as Time,
        value: c.volume || 0,
        color: c.close >= c.open ? "#08998188" : "#f2364588",
      }));
      cs.setData(cData);
      vs.setData(vData);
      chart.timeScale().fitContent();
    }

    const handleResize = () => {
      if (chartContainerRef.current && chart) {
        chart.applyOptions({
          width: chartContainerRef.current.clientWidth,
          height: chartContainerRef.current.clientHeight,
        });
      }
    };

    // Use ResizeObserver for container size changes (responsive breakpoints)
    const resizeObserver = new ResizeObserver(handleResize);
    resizeObserver.observe(chartContainerRef.current);

    window.addEventListener("resize", handleResize);

    return () => {
      resizeObserver.disconnect();
      window.removeEventListener("resize", handleResize);
      chart.remove();
    };
  }, [sanitizedCandles]);

  const isUp = changePercent >= 0;

  return (
    <div className="relative w-full bg-[#131722] rounded-md border border-[#2a2e39] overflow-hidden">
      {/* Top Bar */}
      <div className="flex items-center justify-between px-4 py-2 border-b border-[#2a2e39] bg-[#1e222d]">
        <div className="flex items-center gap-3">
          <span className="text-sm font-semibold text-[#d1d4dc]">{symbol}</span>
          <span
            className={`text-sm font-mono font-semibold ${
              isUp ? "text-[#089981]" : "text-[#f23645]"
            }`}
          >
            ₹{ltp.toFixed(2)}
          </span>
          <span
            className={`text-xs font-semibold px-1.5 py-0.5 rounded ${
              isUp
                ? "bg-[#089981]/20 text-[#089981]"
                : "bg-[#f23645]/20 text-[#f23645]"
            }`}
          >
            {isUp ? "+" : ""}{changePercent.toFixed(2)}%
          </span>
        </div>
        {onTrade && (
          <button
            onClick={onTrade}
            className="flex items-center gap-1.5 bg-[#2962ff] hover:bg-[#1e4bd8] text-white text-xs font-semibold px-3 py-1.5 rounded transition-colors"
          >
            <Zap className="w-3.5 h-3.5" />
            Trade
          </button>
        )}
      </div>
      {/* Chart Container with responsive height: 400px mobile, 500px desktop */}
      <div
        ref={chartContainerRef}
        className="w-full h-[400px] md:h-[500px]"
      />
    </div>
  );
}