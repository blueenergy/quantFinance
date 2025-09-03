from pymongo import MongoClient

client = MongoClient("mongodb://localhost:27017")
db = client["finance"]
#user = db.users.find_one()
#print(user)

#print(db.user_watchlists.find_one())

# query = {
#     "user_id": '689ea9741e5758cc9f847296',
#     "symbol": '000002',
#     'prompt': None,
#     'result': {'$in': [None]}
# }
# db.analysis_history.delete_one(query)

print('*****************************')
query = {
    "user_id": '689ea9741e5758cc9f847296',
    "symbol": '000002'
}
history = list(db.analysis_history.find(query).sort("timestamp", -1))
for h in history:
    print(h)

#print(db.analysis_history.find_one())
