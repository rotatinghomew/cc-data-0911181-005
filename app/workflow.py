from . import staffing
from . import teacher_contracts
from . import admissions

OPERATIONS={
    'staffing.propose':staffing.propose,
    'teacher_contracts.confirm':teacher_contracts.confirm,
    'admissions.publish':admissions.publish,
}
