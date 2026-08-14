from construction_ai.domain.models import Invoice, PurchaseOrder, Quote, Project
from construction_ai.resolution.project import resolve_projects, classification_band
from construction_ai.executive.vertical_slice import prepare_invoice_approval

ORG='ORG-DEMO'
projects=[
    Project('PRJ-0042',ORG,'Wilson Residence','421 8th St E',company_ids=['COMP-0091'],identifiers={'po':['1042-17']}),
    Project('PRJ-0063',ORG,'Parker Residence','900 Main St',company_ids=['COMP-0091']),
]
candidates=resolve_projects(projects,{'po_number':'1042-17','address':'421 8th St E','vendor_company_id':'COMP-0091'})
project=candidates[0]
invoice=Invoice('INV-8831',ORG,'8831','ABC Electric',4760,4533.33,226.67,po_number='1042-17',quote_number='Q-8821',project_id=project.project_id)
po=PurchaseOrder('PO-1',ORG,'1042-17',project.project_id,'COMP-0091',4760,'Q-8821')
quote=Quote('Q-1',ORG,'Q-8821',project.project_id,'COMP-0091',4760,True)
verification, approval=prepare_invoice_approval(invoice,po,quote,duplicate=False,work_confirmed=True)
print({'project':project.project_id,'project_confidence':project.confidence,'band':classification_band(project.confidence),'verification_passed':verification.passed,'approval_status':approval.status.value,'recommended_action':approval.recommended_action})
