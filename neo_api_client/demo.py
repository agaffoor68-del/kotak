# from neo_api_client import NeoAPI
import io
import neo_api_client
import threading


def on_message(message):
    print('[Res]: ', message)


def on_error(message):
    result = message
    print('[OnError]: ', result)


client = neo_api_client.NeoAPI(consumer_key="2409:40f0:7415:4725:c48:d4ff:feef:864e",
                               consumer_secret="34.34.254.159", environment='prod',
                               on_message=on_message,
                               on_error=on_error)
# client = neo_api_client.NeoAPI(access_token='',
#                                environment='prod', on_message=on_message,
#                                on_error=on_error)
client.login(mobilenumber="+918790218191", password="124578")
a = client.session_2fa("33955")
print(a)
# print(client.search_scrip(exchange_segment="nse_fo", symbol="ICICI", expiry="", option_type="CE",strike_price=">505"))
print(client.search_scrip(exchange_segment="nse_fo", symbol="BANKNIFTY", expiry="", option_type="CE",strike_price="45000"))
# client.subscribe_to_orderfeed()
# print(type(a))
