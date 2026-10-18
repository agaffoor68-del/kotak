"use client";

import { useEffect, useRef } from "react";
import { createChart, ColorType, IChartApi, ISeriesApi, CandlestickData, LineData, Time } from "lightweight-charts";
import type { Candle } from "@/lib/api"; // Candle has: open, high, low, close, timestamp

interface LightweightChartProps {
  candles: Candle[];
  height?: number;
  showEMA20?: boolean;
  showEMA50?: boolean;
  showVWAP?: boolean;
}

function calculateEMA(candles: Candle[], period: number): LineData<Time>[] {
  if (candles.length < period) return [];
  const k = 2 / (period + 1);
  const result: LineData<Time>[] = [];
  
  // Calculate initial SMA
  let sum = 0;
  for (let i = 0; i < period; i++) {
    sum += candles[i].close;
  }
  let prevEMA = sum / period;
  
  const parseTime = (t: string): Time => Math.floor(new Date(t).getTime() / 1000) as Time;
  
  result.push({
    time: parseTime(candles[period - 1].timestamp),
    value: prevEMA,
  });

  for (let i = period; i < candles.length; i++) {
    const currentEMA = (candles[i].close - prevEMA) * k + prevEMA;
    result.push({
      time: parseTime(candles[i].timestamp),
      value: currentEMA,
    });
    prevEMA = currentEMA;
  }
  return result;
}

function calculateVWAP(candles: Candle[]): LineData<Time>[] {
  let cumulativeTypicalVolume = 0;
  let cumulativeVolume = 0;
  const result: LineData<Time>[] = [];
  const parseTime = (t: string): Time => Math.floor(new Date(t).getTime() / 1000) as Time;

  for (const c of candles) {
    const typicalPrice = (c.high + c.low + c.close) / 3;
    const vol = c.volume || 1;
    cumulativeTypicalVolume += typicalPrice * vol;
    cumulativeVolume += vol;
    result.push({
      time: parseTime(c.timestamp),
      value: cumulativeTypicalVolume / cumulativeVolume,
    });
  }
  return result;
}

export function TradingViewChart({
  candles,
  height = 400,
  showEMA20 = true,
  showEMA50 = true,
  showVWAP = true,
}: LightweightChartProps) {
  const chartContainerRef = useRef<HTMLDivElement>(null);
  const chartRef = useRef<IChartApi | null>(null);

  useEffect(() => {
    if (!chartContainerRef.current) return;

    const chart = createChart(chartContainerRef.current, {
      width: chartContainerRef.current.clientWidth,
      height,
      layout: {
        background: { type: ColorType.Solid, color: "#0B0E14" },
        textColor: "#94A3B8",
      },
      grid: {
        vertLines: { color: "#1E293B" },
        horzLines: { color: "#1E293B" },
      },
      timeScale: {
        timeVisible: true,
        secondsVisible: false,
      },
    });

    const candlestickSeries = chart.addCandlestickSeries({
      upColor: "#22C55E",
      downColor: "#EF4444",
      borderVisible: false,
      wickUpColor: "#22C55E",
      wickDownColor: "#EF4444",
    });

    const parseTime = (t: string): Time => Math.floor(new Date(t).getTime() / 1000) as Time;

    const sorted = [...candles].sort((a, b) => new Date(a.timestamp).getTime() - new Date(b.timestamp).getTime());

    const formattedData: CandlestickData[] = sorted.map((c) => ({
      time: parseTime(c.timestamp),
      open: c.open,
      high: c.high,
      low: c.low,
      close: c.close,
    }));

    candlestickSeries.setData(formattedData);

    if (showEMA20) {
      const ema20Series = chart.addLineSeries({
        color: "#3B82F6",
        lineWidth: 2,
        title: "EMA 20",
      });
      ema20Series.setData(calculateEMA(sorted, 20));
    }

    if (showEMA50) {
      const ema50Series = chart.addLineSeries({
        color: "#F59E0B",
        lineWidth: 2,
        title: "EMA 50",
      });
      ema50Series.setData(calculateEMA(sorted, 50));
    }

    if (showVWAP) {
      const vwapSeries = chart.addLineSeries({
        color: "#EC4899",
        lineWidth: 2,
        title: "VWAP",
      });
      vwapSeries.setData(calculateVWAP(sorted));
    }

    chart.timeScale().fitContent();
    chartRef.current = chart;

    const handleResize = () => {
      if (chartContainerRef.current && chart) {
        chart.applyOptions({ width: chartContainerRef.current.clientWidth });
      }
    };

    window.addEventListener("resize", handleResize);

    return () => {
      window.removeEventListener("resize", handleResize);
      chart.remove();
    };
  }, [candles, height, showEMA20, showEMA50, showVWAP]);

  return <div ref={chartContainerRef} className="w-full relative" />;
}
