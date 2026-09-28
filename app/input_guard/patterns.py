"""Rule catalogue for manipulation attempts (ESC-13), in Spanish, Portuguese and English.

Every message is folded first: Unicode compatibility forms (fullwidth letters), zero-width
characters removed, lowercase, accents stripped. Patterns then run on one of these views:

- ``text``: punctuation turned into spaces (except ``: - < > / '``), whitespace collapsed.
- ``collapsed``: like ``text``, after joining runs of single letters separated by single
  spaces, so "i g n o r a   t u s" reads "ignora tus". Only for comparison; the letters of
  words split this way are never shown to anyone.
- ``raw``: punctuation kept, for markup that punctuation stripping would erase: bracketed
  system tags ("[SISTEMA]", "<system>") and forced decision fields ("outcome": "RESOLVE").
- Staff impersonation runs on ``text`` without quoted segments, and a match is discarded
  when reported speech precedes it ("me llamó alguien diciendo que era del equipo de
  fraude…"). A customer telling what a scammer said is a fraud report (ESC-03), not an
  impersonation attempt.

Patterns aim at intent, not at single words, to keep known false positives out: "olvidé las
instrucciones del comercio" (a customer describing a merchant) or "veo movimientos de otra
persona en mi cuenta" (an unrecognized-charge report) must not match. Recall on free text is
measured in M17. Known gaps: leetspeak is not handled (it would raise false positives), and
spaced letters are only joined when words are separated by more than one space.
"""

from __future__ import annotations

import re
import unicodedata
from dataclasses import dataclass
from enum import StrEnum


class Category(StrEnum):
    INSTRUCTION_OVERRIDE = "instruction_override"
    STAFF_IMPERSONATION = "staff_impersonation"
    OTHER_CUSTOMER_DATA = "other_customer_data"
    PROMPT_EXTRACTION = "prompt_extraction"


class View(StrEnum):
    TEXT = "text"
    RAW = "raw"
    FIRST_PERSON = "first_person"


@dataclass(frozen=True)
class Pattern:
    pattern_id: str
    category: Category
    regex: re.Pattern[str]
    view: View = View.TEXT


# --- Normalization ---------------------------------------------------------------------------

_ZERO_WIDTH = re.compile("[\u200b-\u200f\u2060\ufeff]")
_PUNCTUATION = re.compile(r"[^\w\s:\-<>/']")
_SPACES = re.compile(r"\s+")
_SPACED_LETTERS = re.compile(r"(?<!\S)\w(?: \w){2,}(?!\S)")
_QUOTED = re.compile(r"[\"“”«»][^\"“”«»]*[\"“”«»]|(?:(?<=\s)|(?<=:)|^)'[^']{3,}'")


def fold(text: str) -> str:
    folded = _ZERO_WIDTH.sub("", unicodedata.normalize("NFKC", text)).lower()
    decomposed = unicodedata.normalize("NFKD", folded)
    return "".join(char for char in decomposed if not unicodedata.combining(char))


def _strip(folded: str) -> str:
    return _SPACES.sub(" ", _PUNCTUATION.sub(" ", folded)).strip()


def normalize(text: str) -> str:
    return _strip(fold(text))


def _join_run(match: re.Match[str]) -> str:
    letters = match.group(0).split(" ")
    # A trailing "y" is usually the Spanish conjunction after the spaced word.
    if len(letters) > 3 and letters[-1] == "y":
        return "".join(letters[:-1]) + " y"
    return "".join(letters)


def collapse_spaced_letters(folded: str) -> str:
    return _strip(_SPACED_LETTERS.sub(_join_run, _PUNCTUATION.sub(" ", folded)))


def _any(*words: str) -> str:
    return "(?:" + "|".join(words) + ")"


# --- Instruction override ---------------------------------------------------------------------

_OVERRIDE_VERB_ES = _any(
    "ignora", "ignore", "ignoren", "olvida", "olvide", "olviden", "omite", "omita", "omitan",
    "descarta", "descarte", "anula", "anule", "desactiva", "desactive", "saltate", "saltese",
    "no sigas", "no siga", "no sigan", "deja de seguir", "deje de seguir",
)  # fmt: skip
_RULE_NOUN_ES = _any(
    "instrucciones", "reglas", "restricciones", "indicaciones", "politicas", "directrices",
    "limites", "filtros",
)  # fmt: skip
_OWNED_OR_PRIOR_ES = (
    rf"(?:(?:tus|sus) (?:\w+ )?{_RULE_NOUN_ES}"
    rf"|{_RULE_NOUN_ES} (?:anteriores|previas|del sistema|de sistema|originales|iniciales"
    r"|internas|de seguridad|que te dieron|que te dio|que tienes|que recibiste)"
    r"|todo lo anterior|lo anterior|todo lo que te dijeron)"
)
_OVERRIDE_VERB_PT = _any(
    "ignore", "ignora", "ignorem", "esqueca", "esquece", "esquecam", "desconsidere",
    "desconsidera", "descarte", "descarta", "anule", "desative", "desativa", "pule",
    "nao siga", "pare de seguir",
)  # fmt: skip
_RULE_NOUN_PT = _any(
    "instrucoes", "regras", "restricoes", "orientacoes", "politicas", "diretrizes", "limites",
    "filtros",
)  # fmt: skip
_OWNED_OR_PRIOR_PT = (
    rf"(?:(?:suas|tuas) (?:\w+ )?{_RULE_NOUN_PT}"
    rf"|{_RULE_NOUN_PT} (?:anteriores|previas|do sistema|de sistema|originais|iniciais"
    r"|internas|de seguranca|que te deram|que voce tem|que voce recebeu)"
    r"|tudo acima|tudo o que foi dito|o que foi dito antes|tudo o que te disseram"
    r"|o que te disseram)"
)
_ROLE_ES = _any(
    "agente", "asesor", "asesora", "gerente", "supervisor", "supervisora", "empleado",
    "empleada", "humano", "banco", "sistema", "administrador", "administradora", "analista",
    "ejecutivo", "ejecutiva", "operador", "operadora",
)  # fmt: skip
_ROLE_PT = _any(
    "atendente", "agente", "gerente", "supervisor", "supervisora", "funcionario", "funcionaria",
    "humano", "banco", "sistema", "administrador", "administradora", "analista", "operador",
    "operadora",
)  # fmt: skip
_DECISION_KEY = _any(
    "outcome", "refund", "decision", "resultado", "reembolso", "estorno", "tier", "status",
    "estado", "approved", "aprobado", "aprovado", "escalate", "escalar", "queue",
)  # fmt: skip
_DECISION_VALUE = _any(
    "resolve", "true", "approved", "aprobad", "aprovad", "refund", "reembols", "estorn", "t1",
    "false", "none", "closed", "cerrad", "fechad",
)  # fmt: skip

_SAY_ES = _any(
    "que respondo", "que digo", "que contesto", "que le digo", "que les digo", "que escribo",
    "que pongo", "como respondo", "como contesto",
    r"(?:que|como) (?:tengo|debo|deberia|puedo) (?:que )?(?:responder|decir|contestar|escribir"
    r"|poner)",
    r"(?:dime|digame|dimelo) (?:que|como) (?:decir|responder|contestar|escribir|poner)",
)  # fmt: skip
_OUTCOME_VERB_ES = _any(
    "escalen", "escale", "aprueben", "apruebe", "acepten", "acepte", "detecten", "marquen",
    "rechacen", "revisen", "transfieran", "cierren", "reembolsen", "paguen",
)  # fmt: skip
_SAY_PT = _any(
    "o que (?:eu )?respondo", "o que (?:eu )?digo", "o que (?:eu )?falo", "o que (?:eu )?escrevo",
    "como (?:eu )?respondo",
    r"(?:o que|como) (?:eu )?(?:tenho|devo|deveria|posso) (?:que )?(?:responder|dizer|falar"
    r"|escrever)",
    r"me (?:diga|fala|fale) (?:o que|como) (?:dizer|responder|falar|escrever)",
)  # fmt: skip
_OUTCOME_VERB_PT = _any(
    "escalem", "escalarem", "aprovem", "aprovarem", "aceitem", "aceitarem", "detectem",
    "detectarem", "marquem", "marcarem", "recusem", "transfiram", "fechem", "reembolsem",
)  # fmt: skip

# --- Staff impersonation --------------------------------------------------------------------

_STAFF_ES = _any(
    "gerente", "supervisor", "supervisora", "administrador", "administradora", "agente",
    "empleado", "empleada", "ejecutivo", "ejecutiva", "analista", "auditor", "auditora",
    "desarrollador", "desarrolladora", "programador", "programadora", "ingeniero", "ingeniera",
    "funcionario", "funcionaria", "operador", "operadora",
)  # fmt: skip
_STAFF_PT = _any(
    "gerente", "supervisor", "supervisora", "administrador", "administradora", "atendente",
    "funcionario", "funcionaria", "analista", "auditor", "auditora", "desenvolvedor",
    "desenvolvedora", "programador", "programadora", "engenheiro", "engenheira", "operador",
    "operadora",
)  # fmt: skip
_STAFF_EN = _any(
    "manager", "supervisor", "admin", "administrator", "employee", "developer", "engineer",
    "agent", "operator", "auditor",
)  # fmt: skip
_BANK_UNIT_ES = (
    r"(?:del banco|de este banco|interno|interna|de sistemas|de soporte|de seguridad"
    r"|de fraude|de fraudes|de ti|de tecnologia|de la entidad)"
)
_BANK_UNIT_PT = (
    r"(?:do banco|deste banco|interno|interna|de sistemas|de suporte|de seguranca|de fraude"
    r"|de fraudes|de ti|de tecnologia|da instituicao)"
)
_TEAM_ES = r"(?:equipo|area|departamento|unidad|central|mesa)"
_TEAM_PT = r"(?:equipe|area|departamento|time|setor|central|unidade)"
_TEAM_TOPIC = (
    r"(?:de )?(?:fraude|fraudes|seguridad|seguranca|sistemas|ti|soporte|suporte|tecnologia"
    r"|prevencion (?:de|a) fraudes?|prevencao (?:de|a) fraudes?|riesgos|riscos|disputas"
    r"|contestacoes)"
)
# Reporting verbs only: generic nouns ("alguien", "WhatsApp") would let an attacker shield a
# first-person claim for free, and quoted messages are already removed by the FIRST_PERSON view.
# The exception stays evadable on purpose (policy §17): not flagging fraud victims matters more,
# and impersonation grants nothing because GATE-04 is enforced in the Tool Layer.
_REPORTED_SPEECH = re.compile(
    r"\b(?:diciendo|me dijo|me dijeron|dijo que|dijeron que|decia que|decian que"
    r"|afirmo que|afirmaron que|aseguro que|aseguraron que|se hizo pasar|se hacia pasar"
    r"|se hicieron pasar|se identifico como|se presento como"
    r"|dizendo|me disse|me disseram|disse que|disseram que|dizia que|afirmou que"
    r"|se passou por|se passando por|se passaram por|se identificou como|se apresentou como"
    r"|saying|said that|told me|claimed|claiming|pretending)\b"
)
REPORTED_SPEECH_WINDOW = 80
"""Characters before an impersonation match searched for reported speech."""

# --- Other customers' data ----------------------------------------------------------------

_REQUEST_ES = _any(
    "dame", "deme", "denme", "dime", "digame", "muestrame", "muestreme", "muestra", "muestre",
    "consulta", "consultame", "consulte", "busca", "buscame", "busque", "revisa", "revise",
    "pasame", "paseme", "enviame", "envieme", "listame", "abre", "abreme", "accede", "acceda",
    r"(?:puedes|puede|podrias|podria|pueden) (?:darme|mostrarme|decirme|consultar|buscar"
    r"|revisar|pasarme|enviarme|abrir|acceder)",
)  # fmt: skip
_DATA_ES = _any(
    "datos", "informacion", "saldo", "saldos", "movimientos", "transacciones", "cuenta",
    "cuentas", "tarjeta", "tarjetas", "numero de tarjeta", "documento", "cedula", "direccion",
    "telefono", "correo", "historial", "productos", "casos", "disputas",
)  # fmt: skip
_THIRD_PARTY_ES = _any(
    "otro cliente", "otra clienta", "otros clientes", "otras clientas", "otra persona",
    "otras personas", "terceros", "un tercero", "otro usuario", "otros usuarios",
    "alguien mas", "del cliente", "de la clienta", "del titular", "de la titular",
    "del usuario", "de ese cliente", "de este cliente", "de esa persona", "de esta persona",
    "cuenta ajena", "cuentas ajenas", "ajenos", "ajenas",
)  # fmt: skip
_REQUEST_PT = _any(
    "me de", "me da", "me diga", "me mostre", "mostre", "mostra", "consulte", "consulta",
    "busque", "procure", "verifique", "me passe", "passa", "passe", "me envie", "envie",
    "liste", "abra", "acesse",
    r"(?:pode|poderia|voce pode|consegue) (?:me dar|me mostrar|me dizer|consultar|buscar"
    r"|verificar|me passar|me enviar|abrir|acessar)",
    r"(?:preciso|quero) que (?:voce )?me (?:mostre|passe|de|diga|envie)",
)  # fmt: skip
_DATA_PT = _any(
    "dados", "informacoes", "saldo", "saldos", "movimentacoes", "transacoes", "extrato",
    "conta", "contas", "cartao", "cartoes", "numero do cartao", "documento", "cpf", "endereco",
    "telefone", "email", "historico", "produtos", "casos", "contestacoes",
)  # fmt: skip
_THIRD_PARTY_PT = _any(
    "outro cliente", "outra cliente", "outros clientes", "outras clientes", "outra pessoa",
    "outras pessoas", "terceiros", "um terceiro", "outro usuario", "outros usuarios",
    "alguem mais", "de outra pessoa", "do cliente", "da cliente", "do titular", "da titular",
    "do usuario", "desse cliente", "deste cliente", "dessa pessoa", "desta pessoa",
)  # fmt: skip
_CUSTOMER_ID = r"\bcli-[a-z0-9]{6,}\b"
_DOCUMENT_REF_ES = (
    r"\b(?:cliente|clienta|titular|usuario|persona) (?:con|de) (?:el )?"
    r"(?:documento|cedula|dni|pasaporte|cc|ce|numero de documento)\b"
)
_DOCUMENT_REF_PT = (
    r"\b(?:cliente|titular|usuario|pessoa) (?:com|de) (?:o )?"
    r"(?:documento|cpf|rg|passaporte|numero do documento)\b"
)

# --- Prompt extraction --------------------------------------------------------------------

_REVEAL_ES = _any(
    "muestrame", "muestreme", "muestra", "dime", "digame", "revela", "revele", "repite",
    "repita", "imprime", "imprima", "copia", "copie", "escribe", "escriba", "comparte",
    "comparta", "ensename", "cual es", "cuales son", "que dice", "que dicen", "lee", "lea",
)  # fmt: skip
_SECRET_ES = _any(
    "prompt", "system prompt", "tus instrucciones", "sus instrucciones",
    r"instrucciones (?:del sistema|de sistema|internas|iniciales|ocultas|originales"
    r"|del inicio|que (?:te|le) (?:dieron|dio)|que recibiste)",
    "mensaje del sistema", "mensaje de sistema", "reglas internas", "configuracion interna",
    "politica interna", "tu programacion", "tus reglas",
)  # fmt: skip
_REVEAL_PT = _any(
    "me mostre", "mostre", "mostra", "me diga", "diga", "revele", "revela", "repita", "repete",
    "imprima", "copie", "escreva", "compartilhe", "me ensine", "qual e", "quais sao",
    "o que diz", "o que dizem", "leia",
)  # fmt: skip
_SECRET_PT = _any(
    "prompt", "system prompt", "suas instrucoes", "tuas instrucoes",
    r"instrucoes (?:do sistema|de sistema|internas|iniciais|ocultas|originais|do inicio"
    r"|que (?:voce )?recebeu|que te deram|que lhe deram)",
    "mensagem do sistema", "mensagem de sistema", "regras internas", "configuracao interna",
    "politica interna", "sua programacao", "suas regras",
)  # fmt: skip


def _p(pattern_id: str, category: Category, regex: str, view: View = View.TEXT) -> Pattern:
    return Pattern(pattern_id, category, re.compile(regex), view)


_O = Category.INSTRUCTION_OVERRIDE
_I = Category.STAFF_IMPERSONATION
_C = Category.OTHER_CUSTOMER_DATA
_E = Category.PROMPT_EXTRACTION

PATTERNS: tuple[Pattern, ...] = (
    # Instruction override
    _p("override.ignore_rules.es", _O,
       rf"\b{_OVERRIDE_VERB_ES}\b(?: \w+){{0,4}} {_OWNED_OR_PRIOR_ES}\b"),
    _p("override.ignore_rules.pt", _O,
       rf"\b{_OVERRIDE_VERB_PT}\b(?: \w+){{0,4}} {_OWNED_OR_PRIOR_PT}\b"),
    _p("override.ignore_rules.en", _O,
       r"\b(?:ignore|disregard|forget|bypass|override)\b(?: \w+){0,4} (?:instructions|rules"
       r"|prompt|guidelines|restrictions|everything above|the above)\b"),
    _p("override.new_role.es", _O,
       r"\b(?:a partir de ahora|desde ahora|de ahora en adelante)\b(?: \w+){0,3} (?:eres|seras"
       r"|actua|actuaras|actuas|responde|responderas|te llamas|vas a ser)\b"),
    _p("override.new_role.pt", _O,
       r"\b(?:a partir de agora|de agora em diante|daqui pra frente)\b(?: \w+){0,3} (?:voce e"
       r"|voce sera|sera|aja|atue|responda|se chama|vai ser)\b"),
    _p("override.new_role.en", _O,
       r"\b(?:you are now|from now on you|pretend (?:to be|you are)|act as (?:an?|the) )"),
    _p("override.act_as.es", _O,
       r"\b(?:actua|actue|actuen|comportate|comportese|finge ser|finge que eres|finja ser"
       r"|hazte pasar por|hagase pasar por|simula ser|simule ser|imagina que eres"
       rf"|haz de cuenta que eres) (?:como )?(?:el |la |un |una |mi )?{_ROLE_ES}\b"),
    _p("override.act_as.pt", _O,
       r"\b(?:aja|atue|comporte-se|finja ser|finja que e|se passe por|simule ser"
       rf"|imagine que voce e|faca de conta que e) (?:como )?(?:o |a |um |uma |meu |minha )?"
       rf"{_ROLE_PT}\b"),
    # Help to game the outcome ("qué respondo para que no escalen"), not a preference to
    # avoid a transfer ("prefiero resolverlo por aquí sin que me pasen con un agente").
    _p("override.evade_controls", _O,
       rf"\b{_SAY_ES}\b(?: \w+){{0,4}} para que (?:no )?(?:me |lo |la |le )?{_OUTCOME_VERB_ES}\b"
       rf"|\b{_SAY_PT}\b(?: \w+){{0,4}} para (?:que )?(?:nao )?(?:me |o |a )?"
       rf"{_OUTCOME_VERB_PT}\b"),
    _p("override.special_mode", _O,
       r"\b(?:modo|mode) (?:desarrollador|desenvolvedor|developer|dan|admin|administrador"
       r"|depuracion|depuracao|debug|dios|deus|god|sin restricciones|sem restricoes"
       r"|jailbreak|root)\b|\bjailbreak\b"),
    _p("override.fake_system_message", _O,
       r"^(?:system|sistema|assistant|asistente|assistente|developer|admin)\s*:"
       r"|\bnuevas instrucciones\b|\bnovas instrucoes\b|\bnew instructions\b"),
    _p("override.system_tag", _O,
       r"[\[<{(]\s*/?\s*(?:system|sistema|admin|administrador|developer|desarrollador"
       r"|assistant|asistente|assistente|instructions?|instrucciones|instrucoes|root"
       r"|superusuario)\s*[\]>})]",
       View.RAW),
    _p("override.forced_decision_fields", _O,
       rf"[\"']?{_DECISION_KEY}[\"']?\s*[:=]\s*[\"']?{_DECISION_VALUE}",
       View.RAW),
    _p("override.forced_output.es", _O,
       r"\b(?:responde|contesta|devuelve|escribe|imprime|genera) (?:solo|solamente|unicamente"
       r"|exactamente) (?:con )?(?:este|esta|el siguiente|la siguiente|esto)\b"),
    _p("override.forced_output.pt", _O,
       r"\b(?:responda|devolva|escreva|imprima|gere) (?:so|somente|apenas|exatamente) (?:com )?"
       r"(?:este|esta|o seguinte|a seguinte|isto|isso)\b"),
    # Staff impersonation: first-person claims only (see module docstring)
    _p("impersonation.staff.es", _I,
       rf"\b(?:soy|le habla|te habla|les habla|habla) (?:el |la |un |una |su )?{_STAFF_ES}\b"
       rf"(?: \w+){{0,5}} {_BANK_UNIT_ES}\b",
       View.FIRST_PERSON),
    _p("impersonation.staff.pt", _I,
       rf"\b(?:sou|aqui e|quem fala e|fala) (?:o |a |um |uma |seu |sua )?{_STAFF_PT}\b"
       rf"(?: \w+){{0,5}} {_BANK_UNIT_PT}\b",
       View.FIRST_PERSON),
    _p("impersonation.staff.en", _I,
       rf"\b(?:i am|i'm|im|this is) (?:the |an? |your )?(?:bank )?{_STAFF_EN}\b",
       View.FIRST_PERSON),
    _p("impersonation.internal_team", _I,
       rf"\b(?:soy|somos) (?:del|de la) {_TEAM_ES} {_TEAM_TOPIC}\b"
       rf"|\b(?:sou|somos) (?:da|do) {_TEAM_PT} {_TEAM_TOPIC}\b",
       View.FIRST_PERSON),
    _p("impersonation.works_at_bank", _I,
       rf"\btrabajo (?:en|para|como|con) (?:el |la |un |una )?(?:{_TEAM_ES} {_TEAM_TOPIC}"
       rf"|banco|este banco|{_STAFF_ES})\b"
       rf"|\btrabalho (?:no|na|em|para|como|com) (?:o |a )?(?:{_TEAM_PT} {_TEAM_TOPIC}"
       rf"|banco|este banco|{_STAFF_PT})\b",
       View.FIRST_PERSON),
    _p("impersonation.privileged_access", _I,
       r"\b(?:acceso|acesso|access|privilegios|privilegio|permisos|permissoes) (?:de )?"
       r"(?:administrador|admin|administrativo|interno|root|superusuario)\b"
       r"|\b(?:codigo|credencial|numero) de (?:empleado|funcionario|agente|atendente)\b"
       r"|\bautorizacion interna\b|\bautorizacao interna\b",
       View.FIRST_PERSON),
    # Other customers' data
    _p("other_customer.request.es", _C,
       rf"\b{_REQUEST_ES}\b(?: \w+){{0,6}} {_DATA_ES}\b(?: \w+){{0,6}} {_THIRD_PARTY_ES}\b"),
    _p("other_customer.request.pt", _C,
       rf"\b{_REQUEST_PT}\b(?: \w+){{0,6}} {_DATA_PT}\b(?: \w+){{0,6}} {_THIRD_PARTY_PT}\b"),
    _p("other_customer.request.en", _C,
       r"\b(?:show|give|tell|list|look up|find|send)\b(?: \w+){0,6} (?:data|details|account"
       r"|accounts|balance|transactions|card|information)\b(?: \w+){0,6} (?:another customer"
       r"|other customers|someone else|another person|other people)\b"),
    _p("other_customer.by_id", _C,
       rf"\b(?:{_REQUEST_ES}|{_REQUEST_PT}|show|give|look up|find)\b.{{0,80}}{_CUSTOMER_ID}"),
    _p("other_customer.by_document", _C,
       rf"\b(?:{_REQUEST_ES}|{_REQUEST_PT})\b.{{0,80}}(?:{_DOCUMENT_REF_ES}|{_DOCUMENT_REF_PT})"),
    # Prompt extraction
    _p("extraction.prompt.es", _E, rf"\b{_REVEAL_ES}\b(?: \w+){{0,4}} {_SECRET_ES}\b"),
    _p("extraction.prompt.pt", _E, rf"\b{_REVEAL_PT}\b(?: \w+){{0,4}} {_SECRET_PT}\b"),
    _p("extraction.prompt.en", _E,
       r"\b(?:show|reveal|print|repeat|tell me|what is|what are|output|leak)\b(?: \w+){0,4} "
       r"(?:system prompt|your prompt|your instructions|initial instructions|hidden instructions"
       r"|your rules|system message)\b"),
)  # fmt: skip


@dataclass(frozen=True)
class _Views:
    text: str
    collapsed: str
    raw: str
    first_person: str


def _views(message: str) -> _Views:
    folded = fold(message)
    return _Views(
        text=_strip(folded),
        collapsed=collapse_spaced_letters(folded),
        raw=_SPACES.sub(" ", folded).strip(),
        first_person=_strip(_QUOTED.sub(" ", folded)),
    )


def _first_person_match(pattern: Pattern, text: str) -> bool:
    for match in pattern.regex.finditer(text):
        before = text[max(0, match.start() - REPORTED_SPEECH_WINDOW) : match.start()]
        if not _REPORTED_SPEECH.search(before):
            return True
    return False


def detect(message: str) -> Pattern | None:
    """The first pattern that matches, in catalogue order, or ``None``."""
    views = _views(message)
    if not views.text and not views.raw:
        return None
    for pattern in PATTERNS:
        if pattern.view is View.RAW:
            matched = bool(pattern.regex.search(views.raw))
        elif pattern.view is View.FIRST_PERSON:
            matched = _first_person_match(pattern, views.first_person)
        else:
            matched = any(pattern.regex.search(text) for text in (views.text, views.collapsed))
        if matched:
            return pattern
    return None
