from typing import List, Dict


class AIBrainEvaluator:
    def __init__(self):
        # In-memory leaderboard (Isko baad me database me save kar sakte hain)
        self.leaderboard = {
            "VWAP_Bounce": {"trades": 0, "wins": 0, "pnl": 0.0},
            "EMA_Crossover": {"trades": 0, "wins": 0, "pnl": 0.0},
        }

    def evaluate_strategies(self, live_data: Dict) -> Dict:
        # Yahan logic aayega jo live data ke hisaab se paper trading karega
        # Aur leaderboard update karega
        pass

    def get_best_strategy(self) -> str:
        best_strat = None
        best_win_rate = -1

        for strat, data in self.leaderboard.items():
            if data["trades"] >= 3:  # Minimum 3 trades for validation
                win_rate = data["wins"] / data["trades"]
                if win_rate > best_win_rate:
                    best_win_rate = win_rate
                    best_strat = strat

        return best_strat if best_strat else "WAIT"
