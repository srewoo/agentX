import type { StockQuote } from "../../shared/types";

interface Props {
  quote: StockQuote;
}

export default function StockQuoteCard({ quote }: Props) {
  const isPositive = (quote.change_pct ?? 0) >= 0;
  const priceColor = isPositive ? "text-profit" : "text-loss";
  const changeIcon = isPositive ? "▲" : "▼";

  return (
    <div className="bg-panel rounded-lg border border-border p-3">
      <div className="flex items-start justify-between">
        <div>
          <div className="font-bold text-base text-zinc-100">{quote.symbol}</div>
          {quote.name && (
            <div className="text-xs text-zinc-500 mt-0.5 max-w-[180px] truncate">{quote.name}</div>
          )}
        </div>
        <div className="text-right">
          <div className={`font-bold text-lg ${priceColor}`}>
            {quote.price != null ? `₹${quote.price.toLocaleString("en-IN", { minimumFractionDigits: 2 })}` : "—"}
          </div>
          {quote.change_pct != null && (
            <div className={`text-xs font-medium ${priceColor}`}>
              {changeIcon} {Math.abs(quote.change_pct).toFixed(2)}% ({quote.change != null ? (quote.change >= 0 ? "+" : "") + quote.change.toFixed(2) : "—"})
            </div>
          )}
        </div>
      </div>

      {/* Stats row */}
      <div className="grid grid-cols-3 gap-2 mt-3">
        {[
          { label: "Open", value: quote.open },
          { label: "High", value: quote.high },
          { label: "Low", value: quote.low },
        ].map(({ label, value }) => (
          <div key={label} className="text-center">
            <div className="text-xs text-zinc-500">{label}</div>
            <div className="text-xs font-medium text-zinc-300">
              {value != null ? `₹${value.toLocaleString("en-IN")}` : "—"}
            </div>
          </div>
        ))}
      </div>
    </div>
  );
}
