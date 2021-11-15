import datetime as dt
import time
import logging
import random

from optibook.synchronous_client import Exchange


from math import floor, ceil
from black_scholes import call_value, put_value, call_delta, put_delta, call_vega, put_vega
from libs import calculate_current_time_to_date

exchange = Exchange()
exchange.connect()

logging.getLogger('client').setLevel('ERROR')

force_delta_increase = False
force_delta_decrease = False


def limit_maker_man(strike, stock_value): 
  maxx = 100
  if strike <= stock_value:
    ratio = strike / stock_value
  else:
    ratio = stock_value / strike
  return round(maxx * ratio,0)
  

def trade_would_breach_position_limit(instrument_id, volume, side, position_limit = 100):
    positions = exchange.get_positions()
    position_instrument = positions[instrument_id]

    if side == 'bid':
        return position_instrument + volume > position_limit
    elif side == 'ask':
        return position_instrument - volume < -position_limit
    else:
        raise Exception(f'''Invalid side provided: {side}, expecting 'bid' or 'ask'.''')

def round_down_to_tick(price, tick_size):
    """
    Rounds a price down to the nearest tick, e.g. if the tick size is 0.10, a price of 0.97 will get rounded to 0.90.
    """
    return floor(price / tick_size) * tick_size


def round_up_to_tick(price, tick_size):
    """
    Rounds a price up to the nearest tick, e.g. if the tick size is 0.10, a price of 1.34 will get rounded to 1.40.
    """
    return ceil(price / tick_size) * tick_size


def get_midpoint_value(instrument_id):
    """
    This function calculates the current midpoint of the order book supplied by the exchange for the instrument
    specified by <instrument_id>, returning None if either side or both sides do not have any orders available.
    """
    order_book = exchange.get_last_price_book(instrument_id=instrument_id)

    # If the instrument doesn't have prices at all or on either side, we cannot calculate a midpoint and return None
    if not (order_book and order_book.bids and order_book.asks):
        return None
    else:
        midpoint = (order_book.bids[0].price + order_book.asks[0].price) / 2.0
        print("midpoint:", midpoint)
        return midpoint


def calculate_theoretical_option_value(expiry_date, strike, callput, stock_value, interest_rate, volatility):
    """
    This function calculates the current fair call or put value based on Black & Scholes assumptions.

    expiry_date: dt.date     -  Expiry date of the option
    strike: float            -  Strike price of the option
    callput: str             -  String 'call' or 'put' detailing what type of option this is
    stock_value:             -  Assumed stock value when calculating the Black-Scholes value
    interest_rate:           -  Assumed interest rate when calculating the Black-Scholes value
    volatility:              -  Assumed volatility of when calculating the Black-Scholes value
    """
    time_to_expiry = calculate_current_time_to_date(expiry_date)

    if callput == 'call':
        option_value = call_value(S=stock_value, K=strike, T=time_to_expiry, r=interest_rate, sigma=volatility)
    elif callput == 'put':
        option_value = put_value(S=stock_value, K=strike, T=time_to_expiry, r=interest_rate, sigma=volatility)
    else:
        raise Exception(f"""Got unexpected value for callput argument, should be 'call' or 'put' but was {callput}.""")

    return option_value


def calculate_option_delta(expiry_date, strike, callput, stock_value, interest_rate, volatility):
    """
    This function calculates the current option delta based on Black & Scholes assumptions.

    expiry_date: dt.date     -  Expiry date of the option
    strike: float            -  Strike price of the option
    callput: str             -  String 'call' or 'put' detailing what type of option this is
    stock_value:             -  Assumed stock value when calculating the Black-Scholes value
    interest_rate:           -  Assumed interest rate when calculating the Black-Scholes value
    volatility:              -  Assumed volatility of when calculating the Black-Scholes value
    """
    time_to_expiry = calculate_current_time_to_date(expiry_date)

    if callput == 'call':
        option_value = call_delta(S=stock_value, K=strike, T=time_to_expiry, r=interest_rate, sigma=volatility)
    elif callput == 'put':
        option_value = put_delta(S=stock_value, K=strike, T=time_to_expiry, r=interest_rate, sigma=volatility)
    else:
        raise Exception(f"""Got unexpected value for callput argument, should be 'call' or 'put' but was {callput}.""")

    return option_value


def update_quotes(callput, option_id, theoretical_price, credit, volume, position_limit, tick_size):
    """
    This function updates the quotes specified by <option_id>. We take the following actions in sequence:
        - pull (remove) any current oustanding orders
        - add credit to theoretical price and round to nearest tick size to create a set of bid/ask quotes
        - calculate max volumes to insert as to not pass the position_limit
        - reinsert limit orders on those levels

    Arguments:
        option_id: str           -  Exchange Instrument ID of the option to trade
        theoretical_price: float -  Price to quote around
        credit: float            -  Difference to subtract from/add to theoretical price to come to final bid/ask price
        volume:                  -  Volume (# lots) of the inserted orders (given they do not breach position limits)
        position_limit: int      -  Position limit (long/short) to avoid crossing
        tick_size: float         -  Tick size of the quoted instrument
    """

    # Print any new trades
    trades = exchange.poll_new_trades(instrument_id=option_id)
    for trade in trades:
        print(f'- Last period, traded {trade.volume} lots in {option_id} at price {trade.price:.2f}, side: {trade.side}.')

    # Pull (remove) all existing outstanding orders
    orders = exchange.get_outstanding_orders(instrument_id=option_id)
    
    old_bid_volume = 0
    old_ask_volume = 0
    for order_id, order in orders.items():
        print(f'- Deleting old {order.side} order in {option_id} for {order.volume} @ {order.price:8.2f}.')
        if order.side == 'ask':
            old_ask_volume = order.volume
        elif order.side == 'bid':
            old_bid_volume = order.volume
        exchange.delete_order(instrument_id=option_id, order_id=order_id)

    # Calculate bid and ask price
    bid_price = round_down_to_tick(theoretical_price - credit, tick_size)
    ask_price = round_up_to_tick(theoretical_price + credit, tick_size)
    
    if bid_price == ask_price:
        coin_flip = random.randint(0,1)
        if coin_flip == 0:
            bid_price += 0.1
        else:
            ask_price -= 0.1

    # Calculate bid and ask volumes, taking into account the provided position_limit
    position = exchange.get_positions()[option_id]

    max_volume_to_buy = position_limit - position
    max_volume_to_sell = position_limit + position
    
    
    total_ask_volume = volume
    total_bid_volume = volume
    if position > 0:
        total_ask_volume = volume + old_ask_volume
    elif position <0:
        total_bid_volume = volume + old_bid_volume
    
    bid_volume = min(volume+old_bid_volume, max_volume_to_buy)
    ask_volume = min(volume+old_ask_volume, max_volume_to_sell)
    
    # Insert new limit orders
    if callput == "call":
        if bid_volume > 0 and not trade_would_breach_position_limit(option_id, volume, 'bid',position_limit = position_limit) and not force_delta_decrease:
            print(f'- Inserting bid limit order in {option_id} for {bid_volume} @ {bid_price:8.2f}.')
            exchange.insert_order(instrument_id=option_id, price=bid_price, volume=bid_volume, side='bid', order_type='limit', )
        if ask_volume > 0 and not trade_would_breach_position_limit(option_id, volume, 'ask',position_limit = position_limit) and not force_delta_increase:
            print(f'- Inserting ask limit order in {option_id} for {ask_volume} @ {ask_price:8.2f}.')
            exchange.insert_order(instrument_id=option_id, price=ask_price, volume=ask_volume, side='ask', order_type='limit', )
    elif callput == "put":
        if bid_volume > 0 and not trade_would_breach_position_limit(option_id, volume, 'bid',position_limit = position_limit) and not force_delta_increase:
            print(f'- Inserting bid limit order in {option_id} for {bid_volume} @ {bid_price:8.2f}.')
            exchange.insert_order(instrument_id=option_id, price=bid_price, volume=bid_volume, side='bid', order_type='limit', )
        if ask_volume > 0 and not trade_would_breach_position_limit(option_id, volume, 'ask',position_limit = position_limit) and not force_delta_decrease:
            print(f'- Inserting ask limit order in {option_id} for {ask_volume} @ {ask_price:8.2f}.')
            exchange.insert_order(instrument_id=option_id, price=ask_price, volume=ask_volume, side='ask', order_type='limit', )


def hedge_delta_position(stock_id, options, stock_value):
    """
    This function (once finished) hedges the outstanding delta position by trading in the stock.

    That is:
        - It calculates how sensitive the total position value is to changes in the underlying by summing up all
          individual delta component.
        - And then trades stocks which have the opposite exposure, to remain, roughly, flat delta exposure

    Arguments:
        stock_id: str         -  Exchange Instrument ID of the stock to hedge with
        options: List[dict]   -  List of options with details to calculate and sum up delta positions for
        stock_value: float    -  The stock value to assume when making delta calculations using Black-Scholes
    """

    # A3: Calculate the delta position here
    positions = exchange.get_positions()
    
    total_delta_position = 0

    book = exchange.get_last_price_book(stock_id)
    if not book.bids or not book.asks:
        return
    else:
        best_bid = float(book.bids[0].price)
        best_ask = float(book.asks[0].price)
    for option in options:
      if option['callput'] == 'put':
        position = positions[option['id']]
        print(f"- The current position in the option {option} is {position}.")
        option_delta = calculate_option_delta(expiry_date=option['expiry_date'],
                                                strike = option['strike'], 
                                                callput = option['callput'], 
                                                stock_value=best_ask, 
                                                interest_rate = 0.0, 
                                                volatility = 3.0)
      elif option['callput'] == 'call':
        position = positions[option['id']]
        print(f"- The current position in the option {option} is {position}.")
        option_delta = calculate_option_delta(expiry_date=option['expiry_date'],
                                              strike = option['strike'], 
                                              callput = option['callput'], 
                                              stock_value=best_bid, 
                                              interest_rate = 0.0, 
                                              volatility = 3.0)
      
      delta_position = option_delta * position
      
      total_delta_position += delta_position
        
    print(f'- The current delta position in the stock {stock_id} is {total_delta_position}.')
    stock_position = positions[stock_id]
    print(f'- The current position in the stock {stock_id} is {stock_position}.')

    # A4: Implement the delta hedge here, staying mindful of the overall position-limit of 100, also for the stocks.
     
    volume = int(round(total_delta_position,0)) + stock_position
    
    delta_pos = float(total_delta_position)
    if volume >= 0:
        stock_position_after = stock_position - volume
        if stock_position_after < -100:
            position_exceeded = -100 - stock_position_after
            volume = volume - position_exceeded
        side = 'ask'
        hedge_price = best_bid
    elif volume < 0:
        volume = -volume
        stock_position_after = stock_position + volume
        if stock_position_after > 100:
            position_exceeded = stock_position_after - 100
            volume = volume - position_exceeded
        side = 'bid'
        hedge_price = best_ask
    
    print("delta_pos: ", delta_pos)
    print("stock_position: ", stock_position)
    print("net_delta: ", (delta_pos + stock_position))
    
    if delta_pos + stock_position > 20 or delta_pos + stock_position < -20:
        
        if not trade_would_breach_position_limit(stock_id, volume, side) and volume != 0:
            print(f'''Inserting IOC {side} for {stock_id}: {volume:.0f} lot(s) at price {hedge_price:.2f}.''')
            exchange.insert_order(
                instrument_id=stock_id,
                price=hedge_price,
                volume=volume,
                side=side,
                order_type='ioc')
        else:
            print("- Not hedging.")
            
    else:
        print(f'- Not hedging.')
    return (delta_pos + stock_position)
    



# A2: Not all the options have been entered here yet, include all of them for an easy improvement


STOCK_ID = 'BMW'
OPTIONS = [
    {'id': 'BMW-2021_12_10-050C', 'expiry_date': dt.datetime(2021, 12, 10, 12, 0, 0), 'strike': 50, 'callput': 'call', 'last_ask': 0.0, 'last_bid': 0.0},
    {'id': 'BMW-2021_12_10-050P', 'expiry_date': dt.datetime(2021, 12, 10, 12, 0, 0), 'strike': 50, 'callput': 'put', 'last_ask': 0.0, 'last_bid': 0.0},
    {'id': 'BMW-2022_01_14-050C', 'expiry_date': dt.datetime(2022,  1, 14, 12, 0, 0), 'strike': 50, 'callput': 'call', 'last_ask': 0.0, 'last_bid': 0.0},
    {'id': 'BMW-2022_01_14-050P', 'expiry_date': dt.datetime(2022,  1, 14, 12, 0, 0), 'strike': 50, 'callput': 'put', 'last_ask': 0.0, 'last_bid': 0.0},
    {'id': 'BMW-2021_12_10-075C', 'expiry_date': dt.datetime(2021, 12, 10, 12, 0, 0), 'strike': 75, 'callput': 'call', 'last_ask': 0.0, 'last_bid': 0.0},
    {'id': 'BMW-2021_12_10-075P', 'expiry_date': dt.datetime(2021, 12, 10, 12, 0, 0), 'strike': 75, 'callput': 'put', 'last_ask': 0.0, 'last_bid': 0.0},
    {'id': 'BMW-2022_01_14-075C', 'expiry_date': dt.datetime(2022,  1, 14, 12, 0, 0), 'strike': 75, 'callput': 'call', 'last_ask': 0.0, 'last_bid': 0.0},
    {'id': 'BMW-2022_01_14-075P', 'expiry_date': dt.datetime(2022,  1, 14, 12, 0, 0), 'strike': 75, 'callput': 'put', 'last_ask': 0.0, 'last_bid': 0.0},
    {'id': 'BMW-2021_12_10-100C', 'expiry_date': dt.datetime(2021, 12, 10, 12, 0, 0), 'strike': 100, 'callput': 'call', 'last_ask': 0.0, 'last_bid': 0.0},
    {'id': 'BMW-2021_12_10-100P', 'expiry_date': dt.datetime(2021, 12, 10, 12, 0, 0), 'strike': 100, 'callput': 'put', 'last_ask': 0.0, 'last_bid': 0.0},
    {'id': 'BMW-2022_01_14-100C', 'expiry_date': dt.datetime(2022,  1, 14, 12, 0, 0), 'strike': 100, 'callput': 'call', 'last_ask': 0.0, 'last_bid': 0.0},
    {'id': 'BMW-2022_01_14-100P', 'expiry_date': dt.datetime(2022,  1, 14, 12, 0, 0), 'strike': 100, 'callput': 'put', 'last_ask': 0.0, 'last_bid': 0.0},
]


for option in OPTIONS:
    book = exchange.get_last_price_book(option['id'])
    if not book.bids or not book.asks:
        continue
    else:
        best_bid = float(book.bids[0].price)
        best_ask = float(book.asks[0].price)
        
    option['last_ask'] = best_ask
    option['last_bid'] = best_bid
        
while True:
    print(f'')
    print(f'-----------------------------------------------------------------')
    print(f'TRADE LOOP ITERATION ENTERED AT {str(dt.datetime.now()):18s} UTC.')
    print(f'-----------------------------------------------------------------')

    stock_value = get_midpoint_value(STOCK_ID)
    
    if stock_value is None:
        print('Empty stock order book on bid or ask-side, or both, unable to update option prices.')
        time.sleep(4)
        continue

    for option in OPTIONS:
        
        trades = exchange.poll_new_trades(option['id'])
        a_list = []
        for t in trades:
            print(f"[TRADED {t.instrument_id}] price({t.price}), volume({t.volume}), side({t.side})")
            a_list.append([t.instrument_id, t.price, t.volume, str(t.side)])
        
        print(a_list)
        for item in a_list:
            side = item[3]
            if side == 'bid':
                last_bid = item[1]
                option['last_bid'] = last_bid
            elif side == 'ask':
                last_ask = item[1]
                option['last_ask'] = last_ask
        
        print(f"\nUpdating instrument {option['id']}")
        
        expiry_date = option['expiry_date']
        strike = option['strike']
        callput = option['callput']
        interest_rate = 0.0
        volatility = 3.0
        
        option_limit = limit_maker_man(strike,stock_value)

        theoretical_value = calculate_theoretical_option_value(expiry_date=expiry_date,
                                                               strike=strike,
                                                               callput=callput,
                                                               stock_value=stock_value,
                                                               interest_rate=interest_rate,
                                                               volatility=volatility)

        # A1: Here we ask a fixed credit of 15cts, regardless of what the market circumstances are or which option
        #  we're quoting. That can be improved. Can you think of something better?
        time_to_expiry = calculate_current_time_to_date(expiry_date)
        if callput == 'call':
            option_delta = call_delta(S=stock_value, K=strike, T=time_to_expiry, r=interest_rate, sigma=volatility)
            option_vega = call_vega(S=stock_value, K=strike, T=time_to_expiry, r = interest_rate, sigma = volatility)
        elif callput == 'put':
            option_delta = put_delta(S=stock_value, K=strike, T=time_to_expiry, r=interest_rate, sigma=volatility)
            option_vega = put_vega(S=stock_value, K=strike, T=time_to_expiry, r=interest_rate, sigma=volatility)
        else:
            raise Exception(f"""Got unexpected value for callput argument, should be 'call' or 'put' but was {callput}.""")
        
        if theoretical_value > option['strike']:
            credit1 = theoretical_value - option['strike'] - 0.1
        elif theoretical_value < option['strike']:
            credit1 = option['strike'] - theoretical_value - 0.1
        else:
            credit1 = 0
        
        book = exchange.get_last_price_book(option['id'])
        if len(book.bids)<2 or len(book.asks)<2:
            credit2 = 0
        else:
            best_bid = float(book.bids[1].price)
            best_ask = float(book.asks[1].price)
            best_bid_vol = int(book.bids[1].volume)
            best_ask_vol = int(book.asks[1].volume)
            
            if theoretical_value > best_bid:
                diff_bid = theoretical_value - best_bid
            else:
                diff_bid = best_bid - theoretical_value
                
            if theoretical_value < best_ask:
                diff_ask = best_ask - theoretical_value
            else:
                diff_ask = theoretical_value - best_ask
            
            if diff_bid < diff_ask:
                credit2 = diff_bid
            else:
                credit2 = diff_ask
        
        positions = exchange.get_positions()
        position = positions[option['id']]
        
        print(best_bid_vol, best_ask_vol, position)
        
        if credit1 > credit2:
            credit = credit2
        elif credit1 < credit2:
            credit = credit1     
        
        # A5: Here we are inserting a volume of 3, only taking into account the position limit of 100, are there better
        #  choices?
        print(f'{option} limit is {option_limit}.')
        update_quotes(callput=option['callput'],
                      option_id=option['id'],
                      theoretical_price=theoretical_value,
                      credit=credit,
                      volume=10,
                      position_limit=option_limit,
                      tick_size=0.10)

        # Wait 1/10th of a second to avoid breaching the exchange frequency limit
        time.sleep(0.10)
    

    print(f'\nHedging delta position')
    
  

    net_delta = hedge_delta_position(STOCK_ID, OPTIONS, stock_value)
    
    stock_position = positions[STOCK_ID]
    print(stock_position)
    if stock_position >= 80:
        print("force_delta_increase = True")
        force_delta_increase = True
    elif stock_position <= 80:
        print("force_delta_decrease = False confirmed.")
        force_delta_decrease = False
    if stock_position <= -80:
        print("force_delta_decrease = True")
        force_delta_decrease = True
    elif stock_position >= -80:
        print("force_delta_increase = False confirmed.")
        force_delta_increase = False
    
    print(f'Sleeping for 1 second.')
    time.sleep(1)
