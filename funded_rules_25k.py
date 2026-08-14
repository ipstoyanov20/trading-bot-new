import MetaTrader5 as mt5
from datetime import datetime
import logging

# Set up logging
logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(levelname)s - %(message)s')

class FundedAccountRules25k:
    """
    Challenge and Funded Account Rules Checker for a $25,000 Account.
    """
    def __init__(self):
        # Account Configuration
        self.STARTING_BALANCE = 25000.0
        
        # Challenge Rules
        self.PROFIT_TARGET = 1500.0
        self.MAX_LOSS_LIMIT = 1000.0
        self.DAILY_LOSS_LIMIT = 500.0
        
        # Funded Rules Limits
        self.MAX_WITHDRAWAL = 800.0
        self.REWARD_SHARE = 0.90
        
        # Contract Limits (Indices usually)
        self.MAX_MINI_CONTRACTS = 2
        self.MAX_MICRO_CONTRACTS = 20

    def check_daily_loss(self):
        """
        Check if the account has hit the Daily Loss Limit of $500.
        Returns: (bool, str) - True if safe, False if limit hit.
        """
        if not mt5.initialize():
            return False, "MT5 initialization failed"
            
        account_info = mt5.account_info()
        if account_info is None:
            return False, "Failed to get account info"
            
        # Get start of day balance from history
        today = datetime.now().replace(hour=0, minute=0, second=0, microsecond=0)
        history_deals = mt5.history_deals_get(today, datetime.now())
        
        # Calculate daily PnL
        daily_pnl = 0.0
        if history_deals:
            for deal in history_deals:
                # Include closed trades profit, swaps, commissions
                daily_pnl += deal.profit + deal.swap + deal.commission
                
        # Add current floating PnL
        daily_pnl += account_info.profit
        
        if daily_pnl <= -self.DAILY_LOSS_LIMIT:
            msg = f"WARNING: Daily loss limit hit! Daily PnL: ${daily_pnl:.2f} Limit: -${self.DAILY_LOSS_LIMIT}"
            logging.error(msg)
            return False, msg
            
        msg = f"Daily Loss Check Passed. Current Daily PnL: ${daily_pnl:.2f}"
        logging.info(msg)
        return True, msg

    def check_max_loss(self):
        """
        Check if the account has hit the Max Loss Limit (EOD) of $1,000.
        Returns: (bool, str) - True if safe, False if limit hit.
        """
        account_info = mt5.account_info()
        if account_info is None:
            return False, "Failed to get account info"
            
        # Trailing max loss calculation based on equity drop from max EOD equity
        # Simplified: Check if equity drops below Starting Balance - Max Loss
        min_allowed_equity = self.STARTING_BALANCE - self.MAX_LOSS_LIMIT
        
        if account_info.equity <= min_allowed_equity:
            msg = f"WARNING: Max loss limit hit! Equity: ${account_info.equity:.2f} Min Allowed: ${min_allowed_equity:.2f}"
            logging.error(msg)
            return False, msg
            
        msg = f"Max Loss Check Passed. Equity: ${account_info.equity:.2f}"
        logging.info(msg)
        return True, msg

    def check_profit_target(self):
        """
        Check if the account has reached the Profit Target of $1,500.
        """
        account_info = mt5.account_info()
        if account_info is None:
            return False, "Failed to get account info"
            
        target_equity = self.STARTING_BALANCE + self.PROFIT_TARGET
        
        if account_info.equity >= target_equity:
            msg = f"SUCCESS: Profit Target reached! Equity: ${account_info.equity:.2f} Target: ${target_equity:.2f}"
            logging.info(msg)
            return True, msg
            
        msg = f"Profit Target not yet reached. Equity: ${account_info.equity:.2f} Target: ${target_equity:.2f}"
        return False, msg
        
    def check_all_rules(self):
        """
        Run all risk management checks.
        """
        daily_safe, daily_msg = self.check_daily_loss()
        max_safe, max_msg = self.check_max_loss()
        target_reached, target_msg = self.check_profit_target()
        
        can_trade = daily_safe and max_safe
        
        status = {
            "can_trade": can_trade,
            "daily_loss_safe": daily_safe,
            "max_loss_safe": max_safe,
            "profit_target_reached": target_reached,
            "messages": [daily_msg, max_msg, target_msg]
        }
        
        return status

if __name__ == "__main__":
    # Test script execution
    if mt5.initialize():
        checker = FundedAccountRules25k()
        print("Checking $25k Funded Account Rules...")
        status = checker.check_all_rules()
        for msg in status["messages"]:
            print(msg)
        
        if not status["can_trade"]:
            print("TRADING HALTED: One or more risk rules violated.")
        else:
            print("Account is in good standing. Trading allowed.")
            
        mt5.shutdown()
    else:
        print("Failed to initialize MT5")
