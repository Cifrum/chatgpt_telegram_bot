from yoomoney import Authorize


# Authorize(
#       client_id="32FCDF0F997CD4FE34613000BCA256A356E7B4A5B3AD57D4B4982AB83B30BFD0",
#       redirect_uri="https://google.com",
#       scope=["account-info",
#              "operation-history",
#              "operation-details",
#              "incoming-transfers",
#              "payment-p2p",
#              "payment-shop",
#              ]
#       )


from yoomoney import Client
token = "4100110907708107.31EE93047D8B24E8DF1955059192812E151E6DB4244478975BE312BD0014788E0807C25975BF0F34FF52D0912D8B23B813F9FA782739351684DA6DD704878C807C3741D9F43610BD027026CA3684AFB672EE08606BA2FA171BF7475320F74C72ED1BC3323B1DB669FAC4552F4990291CC18C8E3CB5FDD9355AB9244E5B29E2BB"
client = Client(token)
history = client.operation_history(label="a1b2c3d4e5")
status = history.operations[0].status
print(status)

# user = client.account_info()
# print("Account number:", user.account)
# print("Account balance:", user.balance)
# print("Account currency code in ISO 4217 format:", user.currency)
# print("Account status:", user.account_status)
# print("Account type:", user.account_type)
# print("Extended balance information:")
# for pair in vars(user.balance_details):
#     print("\t-->", pair, ":", vars(user.balance_details).get(pair))
# print("Information about linked bank cards:")
# cards = user.cards_linked
# if len(cards) != 0:
#     for card in cards:
#         print(card.pan_fragment, " - ", card.type)
# else:
#     print("No card is linked to the account")

# from yoomoney import Quickpay
# quickpay = Quickpay(
#             receiver="4100110907708107",
#             quickpay_form="shop",
#             targets="Подписка GPT-Bot",
#             paymentType="SB",
#             sum=2,
#             label="a1b2c3d4e5"
#             )
# print(quickpay.base_url)