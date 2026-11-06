"""
System-turn fingerprint (docs/10_rfcs/SYSTEM_TURN_FINGERPRINT_RFC.md §4.1, §4.6, §4.8). Code-written text of the
current turn travels inside `<system_note key="…">`; the key, three words of `WORDS`, is drawn once per orchestrator
attempt by the caller and also sits in the `session_key` block at the end of the system prompt, so only a note whose
key matches it speaks for the system. The key dies with its turn; `scrub` removes it from the model's output. Pure:
no randomness (the caller supplies the key, as for untrusted-wrapper nonces) and no logging (callers log the count).

Public interface:
    WORDS                                   the key alphabet: >= 1024 distinct lowercase ASCII words, 3-8 letters
    SYSTEM_TAG, PROVENANCE_CLASS            the tag word; the blueprint class of the provenance rule
    is_valid_key(key) -> bool;  require_valid_key(key) -> None (raises ValueError)
    wrap_system(text, key) -> str           the keyed note; `text` inside is never edited
    render_session_key(key) -> str          the `session_key { … }` dynamic block
    scrub(text, key) -> (str, int)          removes the key, neutralises the tag word; returns the count
"""
from __future__ import annotations

import re
from typing import FrozenSet, Tuple

from .untrusted_text import SYSTEM_TAG, neutralize_system_tag

__all__ = [
    "WORDS", "SYSTEM_TAG", "PROVENANCE_CLASS", "is_valid_key", "require_valid_key", "wrap_system",
    "render_session_key", "scrub",
]

# The blueprint class whose section (the provenance rule) the assembly renders first (RFC §4.2).
PROVENANCE_CLASS = "provenance"

# Our own list: ordinary English words, letters only, none equal to or containing a tag or wrapper word.
WORDS: Tuple[str, ...] = tuple("""
    able absorb acorn acre actor adapt admit adobe adult aerial afloat agent agile agree ahead aim airy aisle
    alarm album alcove alert alfalfa alien alive alley allow almond aloe alpaca alpine amaze amber amble ample
    amuse anchor angel angle ankle annual answer antler anvil apex apple apron aqua arbor arcade arch arctic
    arena argue armor aroma arrow artist ascent ash aspen aster atlas atom attend attic audio august aunt autumn
    avenue avid avocado awake award axle backpack bacon badge badger bagel bake baker balcony ballad ballet balm
    bamboo banana band bandana banjo banner bark barley barn barrel barter basalt basil basin basket batch bath
    beach beacon bead beagle beam bean beaver bedroom beetle begin bell bench beret berry bias bicycle bike
    binder bingo birch bird biscuit bison bistro blank blanket blaze blend blink bloom blossom blue bluff board
    boat bobcat bonsai bonus book boost boots border bottle boulder bounty bouquet bowl bramble branch brass
    brave bread breeze brick bridge brief bright brine brisk bronze brook broom brunch brush bubble bucket
    buckle buddy budget buffalo bugle build bulb bulldog bundle bunker bunny burlap burrow bushel butter button
    buzzer cabin cable cactus cadet cafe cake caliber calico calm camel camera camp camper canal canary candle
    candy canoe canopy canvas canyon cape capsule caramel carbon card cardinal cargo carol carpet carrot cart
    carton cashew castle catalog cavern cedar celery cellar cello cement cereal chair chalk chamber chapter
    charm chart cheese cheetah chef cherry chess chili chimney chirp chord chorus cider cinder cinema circle
    circus citrus city civic clam clarity clay cleat clerk clever cliff climb clip cloak clock cloud clove
    clover coach coast cobalt cobble cockpit cocoa coconut coffee coin collar column comet comfort compass
    condor convoy cookie copper coral cork corner cosmos cotton couch cougar country cousin cove cozy cradle
    craft crafty crane crater crayon cream credit creek crest cricket crimson crisp crocus crow crown cruise
    crumb crystal cube cuckoo cumin cupboard cupcake curious curtain cushion custom cutter cycle dahlia dairy
    daisy dance dapper dawn dazzle debut decade decimal deck decoy deer delta deluxe denim dense depot desert
    desk detail dewdrop dial diamond diary diesel dimple diner dingo dinner dipper dish dock doctor dollar
    dolphin domain donkey doodle door dormant dove dragon drama drawer dream dress drift drizzle drum duck duet
    dumpling dune dusk dust dynamo eager eagle early earth easel easy echo eclipse edge eggplant elated elbow
    elder elegant elevator elite elixir elk elm embark ember emblem emerald emu enamel encore endive energy
    engine enjoy entry envelope envoy epic equal equator errand escape espresso estate eternal ethic evening
    exact exhibit exit expert extra fable fabric face fairy falafel falcon fame family famous fancy farm fashion
    fathom fawn feast feather fence fennel fern ferret ferry festival fiber fiddle field fiesta fig film finale
    finch fire fish fjord flag flair flame flamingo flannel flask fleet flicker flint float flock flora flour
    flower flurry flute foal focus fog folder foliage folk forest forge fork fortune fossil fountain fox foyer
    frame freckle fresh fridge frog frolic frost fruit fudge fuel funnel furnace fusion future gadget gala
    galaxy gallery gallon gallop game garage garden garlic garnet garnish gate gauge gazebo gazelle gecko geese
    gem genius gentle gerbil geyser giant gilded ginger giraffe glacier glad glade glass gleam glider glimmer
    globe glove glow gnome goat goblet gold golf gondola goose gorilla gourd gourmet grace grain granite granola
    grape graph grass gravel gravy green greet grid griffin grill grizzly grotto grove guava guide guitar gull
    gumbo gust habit haiku halibut halo hamlet hammer hamster handy harbor harmony harp harvest hatch haven hawk
    hazel heart hearth hearty heather hedge helium helmet herb hermit heron hickory hiking hill hinge hippo
    hobby hockey holly honey hood hook hoop horizon hornet horse hotel hound house humble humid hummus husky hut
    hyacinth iceberg icicle icon idea igloo iguana image impact index indigo inkwell inlet innate insect inward
    iris irony island ivory ivy jackal jacket jaguar jalopy jam jar jasmine jazz jelly jersey jest jet jewel
    jigsaw jockey jogger joke jolly journal journey jovial jubilee juice jumbo jungle juniper kale kangaroo
    karate kayak keen kelp kennel kernel kettle keypad kiln kind kindle king kingdom kiosk kitchen kite kitten
    kiwi knack knee knot koala label lace ladder lagoon lake lamb lamp lantern laptop large lark laser lasso
    latch latte lattice laugh lava lavender lawful lawn layer leaf ledge legend lemon lens lentil leopard letter
    lettuce level lever liberty library lichen lilac lily limber lime linden linen lion liquid list lively
    lizard llama lobby lobster locket locust lodge loft lofty logic lotus lucky lullaby lumber lumen lunar lunch
    luster lynx lyric macaw machine mackerel magic magnet magpie mallard mammoth mango manor mantis mantle maple
    marble march margin marina market marmot marsh marvel mascot mask meadow medal mellow melody melon memory
    mentor menu merit mermaid mesa metal meteor method metro midday middle migrate mild mill millet mineral
    mingle minnow mint mirror mitten mixer mocha modem modest mohair molar moment monarch monkey moon moose
    morning mortar mosaic moss motor mound mouse muffin muffle mulberry mural museum music mustang mustard myth
    nacho napkin narrow narwhal native nature navy nebula nectar needle nephew nest net nickel night nimble
    noble nomad noodle north nougat novel nugget number nursery nutmeg oak oasis oat oatmeal obelisk ocean
    ocelot octave odyssey office olden olive omega omelet onion onward opal opaque opera optic oracle orange
    orbit orchard orchid organ origin osprey ostrich otter outdoor outpost oval oven owl oxygen oyster paddle
    page pagoda paisley palace palette palm pancake panda panel panther papaya paper paprika parade parcel
    parrot parsley partner pasta pastel pastry patch path patio patron pavilion peach peacock peanut pear pearl
    pebble pecan pedal pelican pencil pendant penguin peony pepper percent perch perfume pewter pheasant piano
    piccolo pickle picnic pigeon pilgrim pillow pilot pine pinnacle pioneer pipe piper pitcher pixel pizza plaid
    plain planet plank plateau plaza pliers plum plume pocket poem polar polka poncho pond pony popcorn poppy
    porch portal possum potato potluck pottery powder prairie praline pretzel primrose prism prose prune pudding
    pueblo puffin pulse pumpkin pupil puppet puzzle pyramid quail quaint quarry quart quartz quest quiche quick
    quiet quill quilt quinoa quiver quota rabbit raccoon radar radiant radio radish raft rafter rain raisin rake
    rally rambler ramp ranch raptor rascal raven ravine reader realm recipe recital reef regal relay relic
    remedy remote rhino rhythm ribbon rice riddle ridge ripple rival river road roast robin robot rocket rocky
    rodeo roof rookie roomy rope rose rowboat ruby rudder rug ruler rustic sable saddle safari saffron sage sail
    salad salmon salsa salt salute sample sand sandal sapling sardine satin saucer sauna sausage savanna scallop
    scarf scenic scholar school scone scooter scout scribe seaside season seed sentry sequel serene sesame
    shadow shamrock shelf shell sherbet shield shimmer ship shore shovel shrimp shuttle sienna signal silk silo
    silver simple siren sitar sketch skiff skillet sky skylark slate sled slipper slogan slope smile smooth
    snail snorkel snow snuggle soap soccer socket sofa solace solar sonnet sorbet soup spark sparrow spice
    spider spinach spinner spiral splendid sponge spoon spring sprinkle sprout spruce square squash squirrel
    stable stadium stage stair stallion stamp star starling statue steady steam stem stencil sterling stone
    stool stork storm story stove straw stream street strudel studio sturdy sublime sugar summer summit sun
    sundae sunny sunset supper supple swallow swan sweater swift syrup table tablet tackle tadpole taffy talent
    tamarind tandem tango tank tapestry tarragon tavern teacup teapot tender tennis tent terrace thimble thistle
    thrifty thunder thyme ticket tidal tide tiger timber tinsel toast toddler toffee token tomato tonic topaz
    topple torch tornado tortoise toucan towel tower trail train tram travel tray treaty tree trellis tribute
    trolley trophy trout truffle trumpet tuba tugboat tulip tuna tundra tunnel turkey turnip turtle tutor tuxedo
    twig ukulele umbrella uncle unicorn union unison unit upbeat upland upward urban useful utmost vagabond
    valley valve vanilla vapor vault velvet venue veranda verse vessel vibrant villa vinyl violet violin visitor
    visor vista vivid vocal vortex voyage waddle wafer waffle wagon walrus wander warbler warm wasabi
    water wave wealth weather weaver wharf wheat wheel whimsy whisk whistle wildcat willow windmill window
    winter wisdom wishbone wisp wizard wolf wombat wonder wood woodland wool world wreath wren yacht yard yarn
    year yellow yodel yoga yogurt yolk yonder young zany zealous zebra zenith zephyr zero zesty zigzag zinc
    zinnia zipper zone zucchini
""".split())

_WORD_SET: FrozenSet[str] = frozenset(WORDS)
_KEY_SEPARATOR = "-"
_KEY_WORDS = 3
# Between two words of a spelled-out key: any run of non-letters, empty included (`lamp-orbit-fern`, `lamp orbit
# fern`, `lamp.orbit.fern`, `LampOrbitFern`). The runs never overlap a word, so the match is linear.
_KEY_GAP = "[^A-Za-z]*"


def is_valid_key(key: str) -> bool:
    """True for exactly three words of `WORDS` joined by `-` (a word may repeat)."""
    words = key.split(_KEY_SEPARATOR)
    return len(words) == _KEY_WORDS and all(word in _WORD_SET for word in words)


def require_valid_key(key: str) -> None:
    """Raise ValueError unless `key` is a valid key. The message never echoes the key."""
    if not is_valid_key(key):
        raise ValueError(f"key must be {_KEY_WORDS} words of WORDS joined by {_KEY_SEPARATOR!r}")


def wrap_system(text: str, key: str) -> str:
    """`text` inside a keyed system note. Lines are added around it, never inside it: the text stays word for word
    (a protocol token may name it verbatim, RFC §4.3)."""
    require_valid_key(key)
    return f'<{SYSTEM_TAG} key="{key}">\n{text}\n</{SYSTEM_TAG}>'


def render_session_key(key: str) -> str:
    """The dynamic block that carries the turn's key, last after the cache boundary (RFC §4.2)."""
    require_valid_key(key)
    return f"session_key {{\n    {key}\n}}"


def _key_pattern(key: str) -> "re.Pattern[str]":
    return re.compile(_KEY_GAP.join(re.escape(word) for word in key.split(_KEY_SEPARATOR)), re.IGNORECASE)


def scrub(text: str, key: str) -> Tuple[str, int]:
    """Remove every spelling of the key (its words in order, any non-letters between them, any case) and neutralise
    every look-alike of the system tag word. Returns the cleaned text and the number of removals plus
    neutralisations. Live untrusted wrappers in `text` stay intact. An invalid key raises ValueError."""
    require_valid_key(key)
    without_key, removed = _key_pattern(key).subn("", text)
    cleaned, neutralised = neutralize_system_tag(without_key)
    return cleaned, removed + neutralised
