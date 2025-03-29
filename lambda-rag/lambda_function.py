import json
import traceback
import boto3
import os
import re
import info

from botocore.config import Config

from langchain_core.prompts import ChatPromptTemplate
from langchain.docstore.document import Document
from langchain_aws import ChatBedrock
from langchain_community.utilities.tavily_search import TavilySearchAPIWrapper
from langchain_community.tools.tavily_search import TavilySearchResults
from langchain_aws import AmazonKnowledgeBasesRetriever
from urllib import parse
from pydantic.v1 import BaseModel, Field

bedrock_region = os.environ.get('bedrock_region')
projectName = os.environ.get('projectName')
path = os.environ.get('sharing_url')

model_name = "Nova Pro"
models = info.get_model_info(model_name)

def isKorean(text):
    # check korean
    pattern_hangul = re.compile('[\u3131-\u3163\uac00-\ud7a3]+')
    word_kor = pattern_hangul.search(str(text))
    # print('word_kor: ', word_kor)

    if word_kor and word_kor != 'None':
        print('Korean: ', word_kor)
        return True
    else:
        print('Not Korean: ', word_kor)
        return False
    
selected_chat = 0
multi_region = 'Disable'
def get_chat():
    global selected_chat, model_type

    profile = models[selected_chat]
    print('profile: ', profile)
    number_of_models = len(models)
        
    bedrock_region =  profile['bedrock_region']
    modelId = profile['model_id']
    model_type = profile['model_type']
    if model_type == 'claude':
        maxOutputTokens = 4096 # 4k
    else:
        maxOutputTokens = 5120 # 5k
    print(f'LLM: {selected_chat}, bedrock_region: {bedrock_region}, modelId: {modelId}, model_type: {model_type}')

    if profile['model_type'] == 'nova':
        STOP_SEQUENCE = '"\n\n<thinking>", "\n<thinking>", " <thinking>"'
    elif profile['model_type'] == 'claude':
        STOP_SEQUENCE = "\n\nHuman:" 
                          
    # bedrock   
    boto3_bedrock = boto3.client(
        service_name='bedrock-runtime',
        region_name=bedrock_region,
        config=Config(
            retries = {
                'max_attempts': 30
            }
        )
    )
    parameters = {
        "max_tokens":maxOutputTokens,     
        "temperature":0.1,
        "top_k":250,
        "top_p":0.9,
        "stop_sequences": [STOP_SEQUENCE]
    }
    # print('parameters: ', parameters)

    chat = ChatBedrock(   # new chat model
        model_id=modelId,
        client=boto3_bedrock, 
        model_kwargs=parameters,
        region_name=bedrock_region
    )    
    if multi_region=='Enable':
        selected_chat = selected_chat + 1
        if selected_chat == number_of_models:
            selected_chat = 0
    else:
        selected_chat = 0

    return chat

numberOfDocs = 3
knowledge_base_name = projectName
s3_prefix = 'docs'
doc_prefix = s3_prefix+'/'

knowledge_base_id = ""
                
class GradeDocuments(BaseModel):
    """Binary score for relevance check on retrieved documents."""

    binary_score: str = Field(description="Documents are relevant to the question, 'yes' or 'no'")

def get_retrieval_grader(chat):
    system = (
        "You are a grader assessing relevance of a retrieved document to a user question."
        "If the document contains keyword(s) or semantic meaning related to the question, grade it as relevant."
        "Give a binary score 'yes' or 'no' score to indicate whether the document is relevant to the question."
    )

    grade_prompt = ChatPromptTemplate.from_messages(
        [
            ("system", system),
            ("human", "Retrieved document: \n\n {document} \n\n User question: {question}"),
        ]
    )    
    structured_llm_grader = chat.with_structured_output(GradeDocuments)
    retrieval_grader = grade_prompt | structured_llm_grader
    return retrieval_grader

def grade_documents(question, documents):
    print("###### grade_documents ######")
    
    print("start grading...")    
    # Score each doc    
    filtered_docs = []
    chat = get_chat()

    for attempt in range(5):
        print(f"attempt: {attempt}")
        try:
            retrieval_grader = get_retrieval_grader(chat)
            for i, doc in enumerate(documents):
                # print('doc: ', doc)
                
                score = retrieval_grader.invoke({"question": question, "document": doc.page_content})
                # print("score: ", score)
                
                grade = score.binary_score
                # print(f"--> grade: {grade}")
                # Document relevant
                if grade.lower() == "yes":
                    print("---GRADE: DOCUMENT RELEVANT---")
                    filtered_docs.append(doc)
                # Document not relevant
                else:
                    print("---GRADE: DOCUMENT NOT RELEVANT---")
                    # We do not include the document in filtered_docs
                    # We set a flag to indicate that we want to run web search
                    continue            
            break
        except Exception:
            err_msg = traceback.format_exc()
            print('error message: ', err_msg)    

    return filtered_docs

contentList = []
def check_duplication(docs):
    global contentList
    length_original = len(docs)
    
    updated_docs = []
    print('length of relevant_docs:', len(docs))
    for doc in docs:            
        if doc.page_content in contentList:
            print('duplicated!')
            continue
        contentList.append(doc.page_content)
        updated_docs.append(doc)            
    length_updated_docs = len(updated_docs)   
    
    if length_original == length_updated_docs:
        print('no duplication')
    else:
        print('length of updated relevant_docs: ', length_updated_docs)
    
    return updated_docs

def search_by_knowledge_base(keyword: str) -> str:
    print("###### search_by_knowledge_base ######")    
    
    global contentList, knowledge_base_id
    contentList = []
 
    print('keyword: ', keyword)
    keyword = keyword.replace('\'','')
    keyword = keyword.replace('|','')
    keyword = keyword.replace('\n','')
    print('modified keyword: ', keyword)
    
    top_k = numberOfDocs
    relevant_docs = []
    filtered_docs = []
    relevant_context = ""

    if knowledge_base_id:    
        try:
            retriever = AmazonKnowledgeBasesRetriever(
                knowledge_base_id=knowledge_base_id, 
                retrieval_config={"vectorSearchConfiguration": {
                    "numberOfResults": top_k,
                    "overrideSearchType": "HYBRID"   # SEMANTIC
                }},
            )
            
            docs = retriever.invoke(keyword)
            print('length of docs: ', len(docs))        
            # print('docs: ', docs)

            print('--> docs from knowledge base')
            for i, doc in enumerate(docs):
                # print_doc(i, doc)
                
                content = f"{keyword}에 대해 조사한 결과는 아래와 같습니다.\n\n"
                if doc.page_content:
                    content = doc.page_content
                
                score = doc.metadata["score"]
                
                link = ""
                if "s3Location" in doc.metadata["location"]:
                    link = doc.metadata["location"]["s3Location"]["uri"] if doc.metadata["location"]["s3Location"]["uri"] is not None else ""
                    
                    # print('link:', link)    
                    pos = link.find(f"/{doc_prefix}")
                    name = link[pos+len(doc_prefix)+1:]
                    encoded_name = parse.quote(name)
                    # print('name:', name)
                    link = f"{path}/{doc_prefix}{encoded_name}"
                    
                elif "webLocation" in doc.metadata["location"]:
                    link = doc.metadata["location"]["webLocation"]["url"] if doc.metadata["location"]["webLocation"]["url"] is not None else ""
                    name = "WEB"

                url = link
                # print('url:', url)
                
                relevant_docs.append(
                    Document(
                        page_content=content,
                        metadata={
                            'name': name,
                            'score': score,
                            'url': url,
                            'from': 'RAG'
                        },
                    )
                )    

            # no grading    
            if len(relevant_docs):
                relevant_context = ""
                for i, document in enumerate(relevant_docs):
                    print(f"{i}: {document}")
                    relevant_context += document.page_content + "\n\n"        
                print('relevant_context: ', relevant_context)     
        except Exception:
            err_msg = traceback.format_exc()
            print('error message: ', err_msg)    

    if not relevant_context:
        relevant_context = "관련된 정보를 찾지 못하였습니다."
    
    return relevant_context


    
def lambda_handler(event, context):
    print('event: ', event)
    
    function = event['function']
    print('function: ', function)

    knowledge_base_name = event["knowledge_base_name"]
    print('knowledge_base_name: ', knowledge_base_name)

    keyword = event.get('keyword')
    print('keyword: ', keyword)

    # retrieve knowledge_base_id
    global knowledge_base_id
    if not knowledge_base_id:
        try: 
            client = boto3.client(
                service_name='bedrock-agent',
                region_name=bedrock_region
            )   
            response = client.list_knowledge_bases(
                maxResults=5
            )
            print('(list_knowledge_bases) response: ', response)
            
            if "knowledgeBaseSummaries" in response:
                summaries = response["knowledgeBaseSummaries"]
                for summary in summaries:
                    if summary["name"] == knowledge_base_name:
                        knowledge_base_id = summary["knowledgeBaseId"]
                        print('knowledge_base_id: ', knowledge_base_id)
        except Exception:
            err_msg = traceback.format_exc()
            print('error message: ', err_msg)    
    
    if function == 'search_rag':
        print('keyword: ', keyword)
        output = search_by_knowledge_base(keyword)
        
    response = {
        'response': output
    }
    print(f"response: {response}")
    
    return response
