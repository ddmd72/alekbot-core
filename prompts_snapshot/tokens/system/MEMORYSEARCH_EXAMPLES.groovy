---
category: examples
class: examples
metadata:
  description: MemorySearchAgent v4 — examples section
  override_by:
  - SYSTEM
  - AGENT
  source: split from COGNITIVE_PROCESS_MEMORY_SEARCH v3
source_file: firestore_utils/uploads/MEMORYSEARCH_EXAMPLES.json
token_id: MEMORYSEARCH_EXAMPLES
uploaded_by: local_script
---

ex_car {
    input: 'SEARCH_REQUEST "What car do I own and what are its key details?"'
    thought_process: '''
        Subject: user's owned vehicle and its details
        Keywords: car, vehicle, auto, registration — short metadata tags; no overlap with queries
        Primary: "car model year specifications details" — what the KB fact itself would say
        Alternative: "plate insurance ownership registration document" — different angle: admin/paperwork side
        Domain: possession
    '''
    output: '{"keywords":["car","vehicle","auto","registration"],"primary_query":"car model year specifications details","alternative_query":"plate insurance ownership registration document","domains":["possession"]}'
}

ex_family {
    input: 'SEARCH_REQUEST "Tell me about my wife and our family situation"'
    thought_process: '''
        Subject: user's wife and family structure
        Keywords: wife, spouse, family, partner — relational tags
        Primary: "wife name age personal background" — what the KB fact about her would say
        Alternative: "married household children family structure" — different angle: family unit/structure
        Domain: network
    '''
    output: '{"keywords":["wife","spouse","family","partner"],"primary_query":"wife name age personal background","alternative_query":"married household children family structure","domains":["network"]}'
}

ex_location {
    input: 'SEARCH_REQUEST "Where do I currently live and where have I lived before?"'
    thought_process: '''
        Subject: current and past residences
        Keywords: location, address, residence, city — place tags
        Primary: "home address city residence country" — what the KB location fact would say
        Alternative: "previous homes relocation moved lived cities" — different angle: history/movement
        Domain: location
    '''
    output: '{"keywords":["location","address","residence","city"],"primary_query":"home address city residence country","alternative_query":"previous homes relocation moved lived cities","domains":["location"]}'
}

ex_diet {
    input: 'SEARCH_REQUEST "What are my food restrictions and what do I like to eat?"'
    thought_process: '''
        Subject: dietary restrictions + food preferences (two aspects)
        Keywords: diet, food, allergy, nutrition — food-related tags
        Primary: "dietary restrictions prohibited foods health condition" — what the KB restriction fact would say
        Alternative: "eating preferences favorite meals cuisine taste" — different angle: what is liked
        Domains: health (restrictions have medical basis) + preference (likes/dislikes)
    '''
    output: '{"keywords":["diet","food","allergy","nutrition"],"primary_query":"dietary restrictions prohibited foods health condition","alternative_query":"eating preferences favorite meals cuisine taste","domains":["health","preference"]}'
}

ex_broad {
    input: 'SEARCH_REQUEST "Give me a full overview of everything you know about me"'
    thought_process: '''
        Subject: complete user profile — genuinely cross-cutting
        Keywords: profile, biography, personal, identity — broad identity tags
        Primary: "biographical facts life summary overview" — what a summary fact would say
        Alternative: "personal traits background history characteristics" — different angle: who they are
        Domain: spans everything — use biographical as closest match
    '''
    output: '{"keywords":["profile","biography","personal","identity"],"primary_query":"biographical facts life summary overview","alternative_query":"personal traits background history characteristics","domains":["biographical"]}'
}
