"""Connector spec data for :mod:`enterprise_connectors`.

Pure ``dict`` literals extracted verbatim from ``enterprise_connectors.py`` so the
logic module (env-filling, read-variant derivation, tool building, catalog) stays
readable. No imports, no logic; appending a connector is still a one-line edit
here. ``_fill_env`` in the parent module normalises env-var names at import time.
"""
from __future__ import annotations

# name, description, **auth-overrides (base_url_env/token_env default to
# <NAME>_BASE_URL / <NAME>_TOKEN -- spell them out only when they differ).
_SPECS: list[dict] = [
    # --- Legal: practice management, time & billing ---
    dict(name="clio",
         description="Clio legal practice-management REST (v4). ops get/post/put/delete "
         "(writes need confirm). Auth: CLIO_BASE_URL (https://app.clio.com) + CLIO_TOKEN "
         "(OAuth bearer)."),
    dict(name="clio_payments", description="Clio Payments legal billing REST (distinct from generic clio entry). ops get/post/put/delete (writes need confirm). Auth: CLIO_PAYMENTS_BASE_URL (https://app.clio.com) + CLIO_PAYMENTS_TOKEN (OAuth bearer)."),
    dict(name="mycase", description="MyCase legal practice management REST. ops get/post/put/delete (writes need confirm). Auth: MYCASE_BASE_URL (https://api.mycase.com) + MYCASE_TOKEN (OAuth bearer)."),
    dict(name="practicepanther", description="PracticePanther legal practice management REST. ops get/post/put/delete (writes need confirm). Auth: PRACTICEPANTHER_BASE_URL (https://app.practicepanther.com) + PRACTICEPANTHER_TOKEN (OAuth bearer)."),
    dict(name="smokeball", description="Smokeball legal practice management REST. ops get/post/put/delete (writes need confirm). Auth: SMOKEBALL_BASE_URL (https://api.smokeball.com) + SMOKEBALL_TOKEN (API key)."),
    dict(name="filevine", description="Filevine legal case management REST. ops get/post/put/delete (writes need confirm). Auth: FILEVINE_BASE_URL (https://api.filevineapp.com) + FILEVINE_TOKEN (OAuth bearer)."),
    dict(name="cosmolex", description="CosmoLex legal practice management REST. ops get/post/put/delete (writes need confirm). Auth: COSMOLEX_BASE_URL + COSMOLEX_TOKEN."),
    dict(name="rocketmatter", description="Rocket Matter legal practice management REST. ops get/post/put/delete (writes need confirm). Auth: ROCKETMATTER_BASE_URL + ROCKETMATTER_TOKEN."),
    dict(name="zolasuite", description="Zola Suite legal practice management REST. ops get/post/put/delete (writes need confirm). Auth: ZOLASUITE_BASE_URL + ZOLASUITE_TOKEN."),
    dict(name="lawcus", description="Lawcus legal practice management REST. ops get/post/put/delete (writes need confirm). Auth: LAWCUS_BASE_URL + LAWCUS_TOKEN."),
    dict(name="actionstep", description="Actionstep legal practice management REST. ops get/post/put/delete (writes need confirm). Auth: ACTIONSTEP_BASE_URL + ACTIONSTEP_TOKEN."),
    dict(name="abacuslaw", description="AbacusLaw (AbacusNext) legal practice management REST. ops get/post/put/delete (writes need confirm). Auth: ABACUSLAW_BASE_URL + ABACUSLAW_TOKEN."),
    dict(name="amicus_attorney", description="Amicus Attorney legal practice management REST. ops get/post/put/delete (writes need confirm). Auth: AMICUS_ATTORNEY_BASE_URL + AMICUS_ATTORNEY_TOKEN."),
    dict(name="leap_legal", description="LEAP legal practice management REST. ops get/post/put/delete (writes need confirm). Auth: LEAP_LEGAL_BASE_URL + LEAP_LEGAL_TOKEN."),
    dict(name="prolaw", description="ProLaw (Thomson Reuters) legal practice management REST. ops get/post/put/delete (writes need confirm). Auth: PROLAW_BASE_URL + PROLAW_TOKEN."),
    dict(name="tabs3", description="Tabs3 legal billing/practice management REST. ops get/post/put/delete (writes need confirm). Auth: TABS3_BASE_URL + TABS3_TOKEN."),
    dict(name="aderant", description="Aderant legal practice/billing management REST. ops get/post/put/delete (writes need confirm). Auth: ADERANT_BASE_URL (https://api.aderant.com) + ADERANT_TOKEN (OAuth bearer)."),
    dict(name="thomson_reuters_elite", description="Thomson Reuters Elite legal billing REST. ops get/post/put/delete (writes need confirm). Auth: THOMSON_REUTERS_ELITE_BASE_URL (https://api.thomsonreuters.com) + THOMSON_REUTERS_ELITE_TOKEN (OAuth bearer)."),
    dict(name="timesolv", description="TimeSolv legal time/billing REST. ops get/post/put/delete (writes need confirm). Auth: TIMESOLV_BASE_URL + TIMESOLV_TOKEN."),
    dict(name="bill4time", description="Bill4Time legal time/billing REST. ops get/post/put/delete (writes need confirm). Auth: BILL4TIME_BASE_URL (https://www.bill4time.com/api) + BILL4TIME_TOKEN."),
    dict(name="leanlaw", description="LeanLaw legal billing REST. ops get/post/put/delete (writes need confirm). Auth: LEANLAW_BASE_URL (https://api.leanlaw.co) + LEANLAW_TOKEN (API key)."),
    dict(name="chrometa", description="Chrometa automatic legal time-tracking REST. ops get/post/put/delete (writes need confirm). Auth: CHROMETA_BASE_URL + CHROMETA_TOKEN."),
    dict(name="intapp", description="Intapp legal/professional services REST. ops get/post/put/delete (writes need confirm). Auth: INTAPP_BASE_URL (https://api.intapp.com) + INTAPP_TOKEN (OAuth bearer)."),
    dict(name="litify", description="Litify (Salesforce-based) legal operations platform REST. ops get/post/put/delete (writes need confirm). Auth: LITIFY_BASE_URL + LITIFY_TOKEN."),
    # --- Legal: research ---
    dict(name="westlaw", description="Westlaw (Thomson Reuters) legal research REST. ops get/post/put/delete (writes need confirm). Auth: WESTLAW_BASE_URL (https://api.thomsonreuters.com) + WESTLAW_TOKEN (OAuth bearer)."),
    dict(name="lexisnexis", description="LexisNexis legal research REST. ops get/post/put/delete (writes need confirm). Auth: LEXISNEXIS_BASE_URL (https://api.lexisnexis.com) + LEXISNEXIS_TOKEN (OAuth bearer)."),
    dict(name="casetext", description="Casetext legal research REST. ops get/post/put/delete (writes need confirm). Auth: CASETEXT_BASE_URL (https://api.casetext.com) + CASETEXT_TOKEN (API key)."),
    dict(name="fastcase", description="Fastcase legal research REST. ops get/post/put/delete (writes need confirm). Auth: FASTCASE_BASE_URL (https://api.fastcase.com) + FASTCASE_TOKEN (API key)."),
    dict(name="vlex", description="vLex legal research REST. ops get/post/put/delete (writes need confirm). Auth: VLEX_BASE_URL (https://vlex.com/api) + VLEX_TOKEN."),
    # --- Legal: court records & litigation analytics ---
    dict(name="pacer", description="PACER federal court records REST. ops get only (federal records; writes need confirm). Auth: PACER_BASE_URL (https://pcl.uscourts.gov) + PACER_TOKEN (login token)."),
    dict(name="unicourt", description="UniCourt court records/litigation data REST. ops get/post/put/delete (writes need confirm). Auth: UNICOURT_BASE_URL (https://unicourt.com/api) + UNICOURT_TOKEN."),
    dict(name="docket_alarm", description="Docket Alarm litigation analytics/docket REST. ops get/post/put/delete (writes need confirm). Auth: DOCKET_ALARM_BASE_URL (https://www.docketalarm.com/api) + DOCKET_ALARM_TOKEN."),
    dict(name="trellis_law", description="Trellis litigation analytics REST. ops get/post/put/delete (writes need confirm). Auth: TRELLIS_LAW_BASE_URL + TRELLIS_LAW_TOKEN."),
    # --- Legal: e-discovery & legal hold ---
    dict(name="everlaw", description="Everlaw e-discovery/litigation REST. ops get/post/put/delete (writes need confirm). Auth: EVERLAW_BASE_URL (https://api.everlaw.com) + EVERLAW_TOKEN (API key)."),
    dict(name="logikcull", description="Logikcull e-discovery REST. ops get/post/put/delete (writes need confirm). Auth: LOGIKCULL_BASE_URL (https://api.logikcull.com) + LOGIKCULL_TOKEN (API key)."),
    dict(name="relativity_ediscovery", description="Relativity e-discovery platform REST. ops get/post/put/delete (writes need confirm). e.g. /Relativity.REST/api. Auth: RELATIVITY_EDISCOVERY_BASE_URL (https://{instance}.relativity.com) + RELATIVITY_EDISCOVERY_TOKEN (OAuth bearer)."),
    dict(name="disco_ediscovery", description="DISCO e-discovery/litigation REST. ops get/post/put/delete (writes need confirm). Auth: DISCO_EDISCOVERY_BASE_URL (https://api.csdisco.com) + DISCO_EDISCOVERY_TOKEN (OAuth bearer)."),
    dict(name="reveal_ediscovery", description="Reveal e-discovery platform REST. ops get/post/put/delete (writes need confirm). Auth: REVEAL_EDISCOVERY_BASE_URL + REVEAL_EDISCOVERY_TOKEN."),
    dict(name="casepoint", description="Casepoint e-discovery/legal hold REST. ops get/post/put/delete (writes need confirm). Auth: CASEPOINT_BASE_URL + CASEPOINT_TOKEN."),
    dict(name="nuix", description="Nuix e-discovery/investigation REST. ops get/post/put/delete (writes need confirm). Auth: NUIX_BASE_URL + NUIX_TOKEN."),
    dict(name="exterro", description="Exterro e-discovery/legal GRC REST. ops get/post/put/delete (writes need confirm). Auth: EXTERRO_BASE_URL + EXTERRO_TOKEN."),
    dict(name="onna", description="Onna e-discovery/knowledge integration REST. ops get/post/put/delete (writes need confirm). Auth: ONNA_BASE_URL (https://api.onna.com) + ONNA_TOKEN."),
    dict(name="zapproved", description="Zapproved (Reveal) legal hold/e-discovery REST. ops get/post/put/delete (writes need confirm). Auth: ZAPPROVED_BASE_URL + ZAPPROVED_TOKEN."),
    # --- Legal: document management & assembly ---
    dict(name="imanage", description="iManage document/matter management REST. ops get/post/put/delete (writes need confirm). e.g. /work/api/v2/customers. Auth: IMANAGE_BASE_URL (https://{instance}.imanage.work) + IMANAGE_TOKEN (OAuth bearer)."),
    dict(name="netdocuments", description="NetDocuments legal document management REST. ops get/post/put/delete (writes need confirm). Auth: NETDOCUMENTS_BASE_URL (https://api.netdocuments.com) + NETDOCUMENTS_TOKEN (OAuth bearer)."),
    dict(name="litera", description="Litera legal document workflow REST. ops get/post/put/delete (writes need confirm). Auth: LITERA_BASE_URL (https://api.litera.com) + LITERA_TOKEN (API key)."),
    dict(name="hotdocs", description="HotDocs legal document automation REST. ops get/post/put/delete (writes need confirm). Auth: HOTDOCS_BASE_URL + HOTDOCS_TOKEN."),
    dict(name="highq", description="HighQ (Thomson Reuters) legal collaboration platform REST. ops get/post/put/delete (writes need confirm). Auth: HIGHQ_BASE_URL + HIGHQ_TOKEN."),
    dict(name="luminance", description="Luminance legal AI document review REST. ops get/post/put/delete (writes need confirm). Auth: LUMINANCE_BASE_URL + LUMINANCE_TOKEN."),
    dict(name="lawdepot", description="LawDepot legal document generation REST. ops get/post/put/delete (writes need confirm). Auth: LAWDEPOT_BASE_URL (https://api.lawdepot.com) + LAWDEPOT_TOKEN (API key)."),
    dict(name="rocket_lawyer", description="Rocket Lawyer legal document/services REST. ops get/post/put/delete (writes need confirm). Auth: ROCKET_LAWYER_BASE_URL (https://api.rocketlawyer.com) + ROCKET_LAWYER_TOKEN (API key)."),
    dict(name="legalzoom", description="LegalZoom legal services REST. ops get/post/put/delete (writes need confirm). Auth: LEGALZOOM_BASE_URL (https://api.legalzoom.com) + LEGALZOOM_TOKEN (API key)."),
    # --- Legal: contract lifecycle ---
    dict(name="ironclad",
         description="Ironclad CLM REST. paths /public/api/v1/... (workflows, records). "
         "ops get/post/put/delete (writes need confirm). Auth: IRONCLAD_BASE_URL "
         "(https://ironcladapp.com) + IRONCLAD_TOKEN (bearer)."),
    dict(name="contractbook", description="Contractbook CLM REST. ops get/post/put/delete (writes need confirm). "
         "Auth: CONTRACTBOOK_BASE_URL (https://api.contractbook.com) + CONTRACTBOOK_TOKEN (bearer)."),
    dict(name="agiloft", description="Agiloft contract lifecycle management REST. ops get/post/put/delete (writes need confirm). Auth: AGILOFT_BASE_URL (https://api.agiloft.com) + AGILOFT_TOKEN (OAuth bearer)."),
    dict(name="concord_clm", description="Concord contract lifecycle management REST. ops get/post/put/delete (writes need confirm). Auth: CONCORD_CLM_BASE_URL (https://api.concordnow.com) + CONCORD_CLM_TOKEN."),
    dict(name="juro", description="Juro contract lifecycle management REST. ops get/post/put/delete (writes need confirm). Auth: JURO_BASE_URL (https://api.juro.com) + JURO_TOKEN."),
    dict(name="sirion_clm", description="SirionLabs contract lifecycle management REST. ops get/post/put/delete (writes need confirm). Auth: SIRION_CLM_BASE_URL (https://api.sirionlabs.com) + SIRION_CLM_TOKEN (OAuth bearer)."),
    dict(name="onit_clm", description="Onit legal operations/CLM REST. ops get/post/put/delete (writes need confirm). Auth: ONIT_CLM_BASE_URL (https://api.onit.com) + ONIT_CLM_TOKEN (API key)."),
    dict(name="lawgeex", description="LawGeex contract review automation REST. ops get/post/put/delete (writes need confirm). Auth: LAWGEEX_BASE_URL (https://api.lawgeex.com) + LAWGEEX_TOKEN (API key)."),
    # --- Legal: matter intake, deadlines & spend ---
    dict(name="lawmatics", description="Lawmatics legal CRM/intake automation REST. ops get/post/put/delete (writes need confirm). Auth: LAWMATICS_BASE_URL (https://api.lawmatics.com) + LAWMATICS_TOKEN."),
    dict(name="lawtoolbox", description="LawToolBox legal deadline/calendar REST. ops get/post/put/delete (writes need confirm). Auth: LAWTOOLBOX_BASE_URL (https://api.lawtoolbox.com) + LAWTOOLBOX_TOKEN (API key)."),
    dict(name="mitratech", description="Mitratech legal operations (TAP) REST. ops get/post/put/delete (writes need confirm). Auth: MITRATECH_BASE_URL + MITRATECH_TOKEN."),
    dict(name="simplelegal", description="SimpleLegal (Onit) legal operations/e-billing REST. ops get/post/put/delete (writes need confirm). Auth: SIMPLELEGAL_BASE_URL + SIMPLELEGAL_TOKEN."),
    dict(name="brightflag", description="Brightflag legal spend management REST. ops get/post/put/delete (writes need confirm). Auth: BRIGHTFLAG_BASE_URL (https://api.brightflag.com) + BRIGHTFLAG_TOKEN (API key)."),
    dict(name="corridor_gtc", description="Corridor legal spend/e-billing REST. ops get/post/put/delete (writes need confirm). Auth: CORRIDOR_GTC_BASE_URL (https://api.corridorcompany.com) + CORRIDOR_GTC_TOKEN (API key)."),
    dict(name="legaltrek", description="LegalTrek legal spend/matter management REST. ops get/post/put/delete (writes need confirm). Auth: LEGALTREK_BASE_URL (https://api.legaltrek.com) + LEGALTREK_TOKEN (API key)."),
    # --- Documents, e-signature & storage ---
    dict(name="docusign",
         description="DocuSign eSignature REST. ops get/post/put/delete (writes need "
         "confirm). e.g. /restapi/v2.1/accounts/{acct}/envelopes. Auth: "
         "DOCUSIGN_BASE_URL + DOCUSIGN_TOKEN."),
    dict(name="adobe_sign", description="Adobe Acrobat Sign e-signature REST. ops get/post/put/delete (writes need confirm). e.g. /api/rest/v6/agreements. Auth: ADOBE_SIGN_BASE_URL (https://api.na1.adobesign.com) + ADOBE_SIGN_TOKEN (OAuth bearer)."),
    dict(name="hellosign", description="HelloSign (Dropbox Sign) e-signature REST (distinct from docusign entry). ops get/post/put/delete (writes need confirm). e.g. /v3/signature_request/send. Auth: HELLOSIGN_BASE_URL (https://api.hellosign.com) + HELLOSIGN_TOKEN (API key).", basic=True),
    dict(name="dropbox_sign", description="Dropbox Sign e-signature REST (rebrand of HelloSign; distinct name entry). ops get/post/put/delete (writes need confirm). Auth: DROPBOX_SIGN_BASE_URL (https://api.hellosign.com) + DROPBOX_SIGN_TOKEN (API key).", basic=True),
    dict(name="pandadoc", description="PandaDoc document/e-signature REST. ops get/post/put/delete (writes need confirm). e.g. /public/v1/documents. Auth: PANDADOC_BASE_URL (https://api.pandadoc.com) + PANDADOC_TOKEN (API key).", token_header="Authorization", scheme="API-Key"),
    dict(name="box",
         description="Box content REST. ops get/post/put/delete (writes need "
         "confirm). e.g. /folders/0/items, /search?query=. Auth: BOX_BASE_URL "
         "(https://api.box.com/2.0) + BOX_TOKEN."),
    dict(name="onedrive", description="Microsoft OneDrive REST (Graph). ops get/post/put/delete (writes need confirm). Auth: ONEDRIVE_BASE_URL (https://graph.microsoft.com) + ONEDRIVE_TOKEN (OAuth2 bearer)."),
    dict(name="sharepoint", description="Microsoft SharePoint REST. ops get/post/put/delete (writes need confirm). Auth: SHAREPOINT_BASE_URL (https://{tenant}.sharepoint.com) + SHAREPOINT_TOKEN."),
    # --- Firm comms & calendar ---
    dict(name="google_calendar",
         description="Google Calendar REST v3. ops get/post/put/delete (writes need "
         "confirm). e.g. /calendars/primary/events, /calendars/primary/events/quickAdd. "
         "Auth: GOOGLE_CALENDAR_BASE_URL (https://www.googleapis.com/calendar/v3) + "
         "GOOGLE_CALENDAR_TOKEN (OAuth2 bearer, refreshed externally)."),
    dict(name="slack_api",
         description="Slack Web API. ops get/post (writes need confirm). e.g. POST "
         "/api/chat.postMessage, GET /api/conversations.list, GET /api/users.list. "
         "Auth: SLACK_API_BASE_URL (https://slack.com) + SLACK_API_TOKEN (bot/user "
         "OAuth bearer, xoxb-.../xoxp-...)."),
    # --- Firm books, billing & payments ---
    dict(name="quickbooks",
         description="QuickBooks Online REST. ops get/post (writes need confirm). "
         "e.g. /v3/company/{id}/query. Auth: QUICKBOOKS_BASE_URL + QUICKBOOKS_TOKEN."),
    dict(name="xero",
         description="Xero accounting REST. ops get/post/put (writes need confirm). "
         "e.g. /api.xro/2.0/Invoices. Auth: XERO_BASE_URL + XERO_TOKEN."),
    dict(name="freshbooks",
         description="FreshBooks accounting REST. ops get/post/put (writes need confirm). "
         "e.g. /accounting/account/{id}/invoices/invoices. Auth: FRESHBOOKS_BASE_URL "
         "(https://api.freshbooks.com) + FRESHBOOKS_TOKEN (OAuth bearer)."),
    dict(name="billdotcom",
         description="Bill.com REST. ops get/post (writes need confirm). Auth: "
         "BILLDOTCOM_BASE_URL + BILLDOTCOM_TOKEN."),
    dict(name="lawpay", description="LawPay legal payment processing REST. ops get/post/put/delete (writes need confirm). Auth: LAWPAY_BASE_URL (https://api.lawpay.com) + LAWPAY_TOKEN (API key)."),
    dict(name="square",
         description="Square REST. ops get/post/put/delete (writes need confirm). Auth: "
         "SQUARE_BASE_URL (https://connect.squareup.com) + SQUARE_TOKEN (bearer)."),
    # --- Tax ---
    dict(name="cch_axcess",
         extra_headers_env={"Ocp-Apim-Subscription-Key": "CCH_AXCESS_SUBSCRIPTION_KEY"},
         description="Wolters Kluwer CCH Axcess Open Integration Platform REST "
         "(Tax / Document / Workstream). ops get/post/put/delete (writes need "
         "confirm). e.g. /api/TaxService/v1.0/..., /api/DocumentService/v1.0/.... "
         "Auth: CCH_AXCESS_BASE_URL (https://api.cchaxcess.com) + CCH_AXCESS_TOKEN "
         "(OAuth bearer) + CCH_AXCESS_SUBSCRIPTION_KEY (Ocp-Apim-Subscription-Key)."),
    dict(name="gosystem_tax",
         description="Thomson Reuters GoSystem Tax RS REST (returns, e-file status, "
         "locators). ops get/post (writes need confirm). Auth: GOSYSTEM_TAX_BASE_URL "
         "(your GoSystem Tax API endpoint) + GOSYSTEM_TAX_TOKEN (OAuth bearer)."),
    dict(name="avalara",
         basic=True, description="Avalara AvaTax REST (sales/use tax). ops get/post (writes "
         "need confirm). e.g. /api/v2/transactions/create, /api/v2/companies. Auth: "
         "AVALARA_BASE_URL (https://rest.avatax.com) + AVALARA_TOKEN (Basic account#:licensekey)."),
    # --- Firm HR ---
    dict(name="gusto",
         description="Gusto REST. ops get/post/put (writes need confirm). e.g. "
         "/v1/companies/{id}/employees. Auth: GUSTO_BASE_URL + GUSTO_TOKEN."),
    # --- Referenced by a domain pack's capability envelope ---
    dict(name="adp",
         description="ADP Workforce Now REST. ops get/post (writes need confirm). e.g. "
         "/hr/v2/workers. Auth: ADP_BASE_URL + ADP_TOKEN."),
    dict(name="carta",
         description="Carta cap-table / equity REST. ops get/post (writes need confirm). "
         "Auth: CARTA_BASE_URL (https://api.carta.com) + CARTA_TOKEN (OAuth bearer)."),
    dict(name="chargebee",
         basic=True, description="Chargebee subscription-billing REST (v2). ops get/post "
         "(writes need confirm). e.g. /api/v2/subscriptions, /api/v2/invoices. Auth: "
         "CHARGEBEE_BASE_URL (https://{site}.chargebee.com) + CHARGEBEE_TOKEN (Basic; API "
         "key as username)."),
    dict(name="concur",
         description="SAP Concur REST. ops get/post/put/delete (writes need confirm). "
         "e.g. /expensereports/v4/reports. Auth: CONCUR_BASE_URL + CONCUR_TOKEN."),
    dict(name="coupa",
         description="Coupa spend/procurement REST. ops get/post/put/delete (writes "
         "need confirm). e.g. /api/requisitions, /api/purchase_orders. Auth: "
         "COUPA_BASE_URL + COUPA_TOKEN."),
    dict(name="modern_treasury", basic=True,
         description="Modern Treasury REST (payments/ledgers/reconciliation). ops get/post/"
         "put/delete (writes need confirm). e.g. /api/payment_orders, /api/counterparties, "
         "/api/internal_accounts. Auth: MODERN_TREASURY_BASE_URL "
         "(https://app.moderntreasury.com) + MODERN_TREASURY_TOKEN (Basic org_id:api_key)."),
    dict(name="netsuite",
         description="Oracle NetSuite SuiteTalk REST. ops get/post/patch/delete (writes "
         "need confirm). e.g. /services/rest/record/v1/salesOrder. Auth: "
         "NETSUITE_BASE_URL + NETSUITE_TOKEN."),
    dict(name="ramp",
         description="Ramp REST (spend). ops get/post/put/delete (writes need confirm). "
         "Auth: RAMP_BASE_URL (https://api.ramp.com) + RAMP_TOKEN (bearer)."),
]

# GraphQL services (single POST endpoint; mutations confirm-gated).
_GRAPHQL_SPECS: list[dict] = [
]
