CREATE TABLE [HumanResources].[Employee] (
    [BusinessEntityID] int NOT NULL,
    [NationalIDNumber] nvarchar(15) NOT NULL,
    [LoginID] nvarchar(256) NOT NULL,
    [OrganizationNode] hierarchyid NULL,
    [OrganizationLevel] smallint NULL,
    [JobTitle] nvarchar(50) NOT NULL,
    [BirthDate] date NOT NULL,
    [MaritalStatus] nchar(1) NOT NULL,
    [Gender] nchar(1) NOT NULL,
    [HireDate] date NOT NULL,
    [SalariedFlag] bit NOT NULL,
    [VacationHours] smallint NOT NULL,
    [SickLeaveHours] smallint NOT NULL,
    [CurrentFlag] bit NOT NULL,
    [rowguid] uniqueidentifier NOT NULL,
    [ModifiedDate] datetime NOT NULL
);
